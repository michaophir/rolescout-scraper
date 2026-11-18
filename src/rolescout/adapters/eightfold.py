"""Eightfold career sites.

Observed on live sites in November 2026; ``tests/fixtures/eightfold_*.json``
hold recorded responses.

- A site lives on the job system's own hostname
  (``https://acme.eightfold.ai/careers``) or on one of the company's
  (``https://careers.acme.com/careers``). Every request names the company by
  its domain: ``?domain=acme.com``. The careers page carries that domain in
  its links, which is how a reference given without one is completed.
- Two list endpoints exist, and a site answers only one of them (the other
  answers 403):

  - ``GET /api/apply/v2/jobs?domain=&start=&num=10&sort_by=timestamp`` gives
    ``{"count": n, "positions": [...]}``;
  - ``GET /api/pcsx/search?domain=&query=&location=&start=&sort_by=timestamp``
    gives ``{"data": {"count": n, "positions": [...]}}`` with other field
    names for the same things.

- Both return ten roles a page whatever is asked for. ``start`` is the
  offset of the page.
- A large list moves while it is read: read in steps of ten, one board of
  2,300 roles repeated ten to twenty roles at page boundaries and left as
  many out. So each page starts two roles before the end of the last one,
  which makes a board cost one request per eight roles.
- The list carries title, locations, department, posted time and the job's
  URL, and no description.
- A site that is asked too often answers 429 "Please try again later".
- robots.txt allows ``/careers``, ``/api/apply`` and ``/api/pcsx`` and
  disallows the rest.
"""

import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlsplit

import requests

from rolescout.adapters.base import BoardIdentity, BoardNotFound, FetchContext, FetchStats, SourceAdapter, SourceError
from rolescout.http import DEFAULT_TIMEOUT
from rolescout.models import Role
from rolescout.text import collapse_whitespace

PAGE_SIZE = 10                 # what both endpoints return per page
PAGE_OVERLAP = 2               # roles of the last page each page reads again, in case the list moved
MIN_INTERVAL = 1.5             # seconds between requests, whatever session is used
MAX_PAGES = 1500               # one board's fetch stops after this many pages
RATE_LIMIT_WAITS = (30.0, 90.0)   # pauses before the retries of a page answered 429
MAX_RETRY_AFTER = 300.0        # the longest pause a Retry-After header can ask for
SLOWER_AFTER_429 = 2.0         # the interval is multiplied by this once a 429 was seen
PAGE_MAX_CHARS = 2_000_000     # of the careers page, read to find the domain

_HOST = r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*[a-z0-9]"
_REF = re.compile(rf"^({_HOST})(?:/({_HOST}))?$", re.I)
_OWN_HOST = re.compile(r"(?:^|\.)eightfold\.ai$", re.I)
# Hosts on the job system's domain that are its own, not a customer's site.
_NOT_A_SITE = {"www", "app", "api", "static", "cdn", "docs", "help", "support", "blog", "status"}
# A URL on the job system's hostname, or on any hostname when it is a careers
# URL that names the company's domain the way these sites do.
EIGHTFOLD_URL = re.compile(
    rf"(?<![\w.-])(?:(?!www\.)[a-z0-9-]+\.eightfold\.ai(?![\w.-])(?:/[^\s\"'<>\\)]*)?"
    rf"|{_HOST}/careers[^\s\"'<>\\)]*[?&]domain=[a-z0-9.-]+)", re.I)
_DOMAIN_IN_PAGE = re.compile(rf"[?&]domain=({_HOST})(?![\w.-])", re.I)


def parse_eightfold_url(url: str) -> str | None:
    """The board a URL belongs to, as ``host/domain`` or just ``host``, or None.

        https://acme.eightfold.ai/careers?domain=acme.com       acme.eightfold.ai/acme.com
        https://acme.eightfold.ai/careers/job/123               acme.eightfold.ai
        https://careers.acme.com/careers?domain=acme.com        careers.acme.com/acme.com
        https://careers.acme.com/api/apply/v2/jobs?domain=acme.com

    A URL on a company's own hostname counts only when it names the domain:
    nothing else about it says which job system is behind it.
    """
    text = (url or "").strip()
    try:
        parts = urlsplit(text if "://" in text else "//" + text.lstrip("/"))
        host = (parts.hostname or "").rstrip(".").lower()
        query = parse_qs(parts.query)
    except ValueError:
        return None
    if not re.fullmatch(_HOST, host):
        return None
    domain = (query.get("domain") or [""])[0].strip().lower().rstrip(".")
    if domain and not re.fullmatch(_HOST, domain):
        domain = ""
    if _OWN_HOST.search(host):
        if host == "eightfold.ai" or host.split(".")[0] in _NOT_A_SITE:
            return None
    elif not domain or not parts.path.startswith(("/careers", "/api/apply", "/api/pcsx")):
        return None
    return f"{host}/{domain}" if domain else host


def parse_ref(ref: str) -> str:
    """A stored reference (``host/domain`` or ``host``) or a URL of the site. Raises ``ValueError``."""
    text = (ref or "").strip()
    if "://" not in text and "?" not in text:
        match = _REF.match(text.rstrip("/"))
        if match:
            return text.rstrip("/").lower()
    parsed = parse_eightfold_url(text)
    if parsed is None:
        raise ValueError(f"{text!r} is not an Eightfold career site: expected its address and the company's "
                         "domain (e.g. acme.eightfold.ai/acme.com), or a URL of the site")
    return parsed


def find_refs(text: str) -> list[str]:
    """The boards named by URLs in ``text``. One that names its domain
    replaces the same host found without it."""
    refs: dict[str, str] = {}      # host -> reference
    for match in EIGHTFOLD_URL.finditer(text):
        ref = parse_eightfold_url("https://" + match.group(0).rstrip(".,;"))
        if ref is None:
            continue
        host = ref.split("/")[0]
        if host not in refs or "/" not in refs[host]:
            refs[host] = ref
    return list(refs.values())


def _date(seconds: object) -> str:
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _places(values: object) -> list[str]:
    if isinstance(values, str):
        values = [values]
    places: list[str] = []
    for value in values if isinstance(values, list) else []:
        place = re.sub(r"\s*,\s*", ", ", collapse_whitespace(str(value or ""))).strip(", ")
        if place and place not in places:
            places.append(place)
    return places


class _Variant:
    """One of the two list endpoints: its URL and where its answer keeps things."""

    def __init__(self, name: str, path: str, query: str) -> None:
        self.name, self.path, self.query = name, path, query

    def url(self, host: str, domain: str, start: int) -> str:
        return f"https://{host}{self.path}?domain={quote(domain)}&start={start}{self.query}"

    def body(self, payload: object) -> dict | None:
        """The object holding ``count`` and ``positions``, or None when the answer is not this endpoint's."""
        if self.name == "search":
            payload = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and isinstance(payload.get("positions"), list):
            return payload
        return None


VARIANTS = (
    _Variant("jobs", "/api/apply/v2/jobs", f"&num={PAGE_SIZE}&sort_by=timestamp"),
    _Variant("search", "/api/pcsx/search", "&query=&location=&sort_by=timestamp"),
)


class EightfoldAdapter(SourceAdapter):
    name = "eightfold"
    # A site's address cannot be made from a company name: many are on the
    # company's own hostname, and all need the company's domain as well.
    guessable = False

    def __init__(self, session: requests.Session | None = None, *, min_interval: float | None = None,
                 max_pages: int = MAX_PAGES, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(session)
        self.min_interval = MIN_INTERVAL if min_interval is None else min_interval
        self.max_pages = max_pages
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self._interval = self.min_interval

    @classmethod
    def canonical_ref(cls, ref: str) -> str:
        return parse_ref(ref)

    def board_url(self, ref: str) -> str:
        host, _, domain = parse_ref(ref).partition("/")
        return f"https://{host}/careers" + (f"?domain={quote(domain)}" if domain else "")

    # -- requests

    def _pace(self) -> None:
        if self._last_request is not None:
            wait = self._interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)

    def _check_robots(self, url: str, ref: str) -> None:
        unread = urlsplit(url).netloc not in self._robots
        if unread:
            self._pace()
        try:
            super()._check_robots(url, ref)
        finally:
            if unread:
                self._last_request = self._clock()

    def _send(self, url: str, ref: str, *, accept: str = "application/json") -> requests.Response:
        """One paced request. A 429 is retried after a pause, and the pace
        slows for the rest of the fetch; one that keeps answering 429 fails
        the fetch. A dropped connection is treated the same way: these sites
        drop the connection of a client they find too busy."""
        for wait_after in (*RATE_LIMIT_WAITS, None):
            self._pace()
            if self.last_stats is not None:
                self.last_stats.requests += 1
            try:
                resp = self.session.get(url, timeout=DEFAULT_TIMEOUT, headers={"Accept": accept})
            except requests.RequestException as exc:
                self._last_request = self._clock()
                if wait_after is None:
                    raise SourceError(f"{self.name}: request for {ref!r} failed: {exc}") from exc
                self._interval = max(self._interval, self.min_interval * SLOWER_AFTER_429)
                self._sleep(wait_after)
                continue
            self._last_request = self._clock()
            if resp.status_code != 429:
                return resp
            self._interval = max(self._interval, self.min_interval * SLOWER_AFTER_429)
            if wait_after is None:
                break
            try:
                asked = float((getattr(resp, "headers", None) or {}).get("Retry-After") or 0)
            except (TypeError, ValueError):
                asked = 0.0
            self._sleep(min(max(asked, wait_after), MAX_RETRY_AFTER))
        raise SourceError(f"{self.name}: {ref!r} keeps answering HTTP 429 (asked too often); "
                          "the fetch was abandoned and nothing was changed")

    def _json(self, resp: requests.Response, ref: str) -> object:
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"{self.name}: non-JSON response for {ref!r}") from exc

    def _domain(self, host: str, ref: str) -> str:
        """The company domain a site's own careers page names."""
        url = f"https://{host}/careers"
        self._check_robots(url, ref)
        resp = self._send(url, ref, accept="text/html")
        if resp.status_code == 404:
            raise BoardNotFound(f"{self.name}: no career site found at {host}")
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: HTTP {resp.status_code} for {ref!r}")
        found = Counter(m.group(1).lower() for m in _DOMAIN_IN_PAGE.finditer((resp.text or "")[:PAGE_MAX_CHARS]))
        if not found:
            raise SourceError(f"{self.name}: {url} does not name the company's domain; "
                              f"give it with the reference, as {host}/COMPANY-DOMAIN")
        return found.most_common(1)[0][0]

    def _first_page(self, ref: str) -> tuple[str, str, _Variant, dict]:
        """(host, domain, the endpoint this site answers, its first page)."""
        host, _, domain = parse_ref(ref).partition("/")
        self._interval = self.min_interval
        if not domain:
            domain = self._domain(host, ref)
        refused = None
        for variant in VARIANTS:
            url = variant.url(host, domain, 0)
            self._check_robots(url, ref)
            resp = self._send(url, ref)
            if resp.status_code in (401, 403, 404):
                refused = resp.status_code
                continue                  # a site answers one endpoint and refuses the other
            if resp.status_code != 200:
                raise SourceError(f"{self.name}: HTTP {resp.status_code} for {ref!r}")
            body = variant.body(self._json(resp, ref))
            if body is None:
                raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
            return host, domain, variant, body
        if refused == 404:
            raise BoardNotFound(f"{self.name}: no board found for {ref!r}")
        raise SourceError(f"{self.name}: HTTP {refused} for {ref!r} on both list endpoints")

    # -- the adapter interface

    def confirm(self, ref: str) -> tuple[str, int]:
        """One page is enough: it names the endpoint and the number of open roles."""
        self.last_stats = FetchStats()
        host, domain, _, body = self._first_page(ref)
        return f"{host}/{domain}", _count(body)

    def fetch_raw(self, ref: str) -> dict:
        """Every page of the board, gathered: ``{host, domain, variant, count, positions}``."""
        if self.last_stats is None:
            self.last_stats = FetchStats()
        stats = self.last_stats
        host, domain, variant, body = self._first_page(ref)
        count = _count(body)
        positions = list(body["positions"])
        start, pages = 0, 1
        while body["positions"] and start + len(body["positions"]) < count:
            if pages >= self.max_pages:
                stats.complete = False
                stats.notes.append(f"stopped after {pages} pages; {count} roles are listed")
                break
            start += max(len(body["positions"]) - PAGE_OVERLAP, 1)
            resp = self._send(variant.url(host, domain, start), ref)
            if resp.status_code != 200:
                raise SourceError(f"{self.name}: HTTP {resp.status_code} for {ref!r} at role {start}")
            body = variant.body(self._json(resp, ref))
            if body is None:
                raise SourceError(f"{self.name}: unexpected response shape for {ref!r} at role {start}")
            count = _count(body) or count       # the number listed can change while the board is read
            positions += body["positions"]
            pages += 1
        payload = {"host": host, "domain": domain, "variant": variant.name, "count": count, "positions": positions}
        # Even overlapping pages rarely add up exactly on a long list; a read
        # well short of what the site lists is not complete.
        stats.count(len(self.items(payload, ref)), count)
        return payload

    def fetch(self, ref: str, *, company: str | None = None,
              context: FetchContext | None = None) -> list[Role]:
        self.last_stats = FetchStats()
        return self.parse(self.fetch_raw(ref), ref, company=company)

    def items(self, payload: object, ref: str) -> list[dict]:
        """The positions, each once: a list read page by page can repeat one."""
        if not isinstance(payload, dict) or not isinstance(payload.get("positions"), list):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        unique: dict[str, dict] = {}
        for item in payload["positions"]:
            if isinstance(item, dict) and item.get("id") not in (None, ""):
                unique.setdefault(str(item["id"]), item)
        return list(unique.values())

    def identity(self, ref: str) -> BoardIdentity:
        _, _, domain = parse_ref(ref).partition("/")
        return BoardIdentity(website=f"https://{domain}" if domain else "")

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        items = self.items(payload, ref)
        host, domain = payload.get("host") or parse_ref(ref).split("/")[0], payload.get("domain") or ""
        roles = []
        for item in items:
            job_id = str(item["id"])
            # The two endpoints name the same things differently.
            places = _places(item.get("standardizedLocations")) or _places(item.get("locations")) \
                or _places(item.get("location"))
            workplace = str(item.get("work_location_option") or item.get("workLocationOption") or "").lower()
            workplace = next((kind for kind in ("remote", "hybrid", "onsite") if workplace.startswith(kind)), "")
            url = str(item.get("canonicalPositionUrl") or item.get("positionUrl") or f"/careers/job/{job_id}")
            if url.startswith("/"):
                url = f"https://{host}{url}"
            roles.append(Role(
                source=self.name,
                source_job_id=job_id,
                company=company or domain or host,
                title=collapse_whitespace(str(item.get("name") or item.get("posting_name") or "")),
                department=collapse_whitespace(str(item.get("department") or "")),
                location="; ".join(places),
                workplace_type=workplace,      # "remote_local" and the like are remote
                url=url,
                date_posted=_date(item.get("postedTs") or item.get("t_create")),
                compensation_text="",
                description="",      # the list has none, and none is fetched
            ))
        return roles


def _count(body: dict) -> int:
    try:
        return max(int(body.get("count") or 0), 0)
    except (TypeError, ValueError):
        return len(body["positions"])
