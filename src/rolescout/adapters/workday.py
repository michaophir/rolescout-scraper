"""Workday career sites (the JSON endpoints their own pages load, under /wday/cxs/).

Workday documents no public API. What is here was observed on live boards in
November 2026; ``tests/fixtures/workday_*.json`` hold recorded responses.

- ``POST /wday/cxs/{tenant}/{site}/jobs`` with ``{"appliedFacets": {},
  "limit": 20, "offset": 0, "searchText": ""}`` lists postings, newest first.
  A limit above 20 is refused (HTTP 400). ``total`` is only filled in on the
  first page; later pages say 0. An offset past the end does not return an
  empty page: it returns the first page again, so paging must stop at ``total``.
- A search never reports more than 2,000 results. A larger board is read by
  filtering on one of the facets the first page lists (country, usually) and
  paging each value separately.
- A posting in the list has a title, a path, a location text ("3 Locations"
  when there are several) and "Posted 5 Days Ago", not a date. Some boards
  leave the location or the posted text out.
- ``GET /wday/cxs/{tenant}/{site}{externalPath}`` returns one posting with its
  description and its exact ``startDate``. That is one request per role, so it
  is made only for the roles the caller wants a description for.
- A wrong site answers 404; a wrong tenant or data center answers 422.
- The tenant in the path is usually the first label of the hostname, but a
  tenant with an underscore has a hyphen in its hostname (tenant ``acme_corp``
  is served from ``acme-corp.wd5.myworkdayjobs.com``).
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from rolescout.adapters.base import (
    BoardIdentity, BoardNotFound, FetchContext, FetchStats, SourceAdapter, SourceError,
)
from rolescout.http import DEFAULT_TIMEOUT, USER_AGENT
from rolescout.models import Role
from rolescout.text import collapse_whitespace, strip_html

PAGE_SIZE = 20                 # the most the endpoint accepts
RESULT_CAP = 2000              # the most one search reports
MIN_INTERVAL = 1.0             # seconds between requests, whatever session is used
BOARD_SECONDS = 900.0          # one board's fetch stops after this long (the largest board seen took ~500)
MAX_REQUESTS = 900             # ... or after this many requests
MAX_DESCRIPTIONS = 150         # descriptions fetched per board per fetch; the rest wait
MAX_SPLIT_VALUES = 100         # a facet with more values than this is not used to split
MAX_SPLIT_DEPTH = 2            # facets combined at most
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_WAIT = 5.0
MAX_RETRY_WAIT = 30.0
DETAIL_FAILURES_TO_STOP = 3    # consecutive failed description requests

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?"
_SITE = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_JOBS_HOST = re.compile(rf"^({_LABEL})\.(wd\d+)\.myworkdayjobs\.com$", re.I)
_SITE_HOST = re.compile(r"^(wd\d+)\.myworkdaysite\.com$", re.I)
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_REF = re.compile(rf"^({_LABEL})/(wd\d+)/({_SITE})$", re.I)
# A Workday URL inside a page. The host part is what makes it one.
WORKDAY_URL = re.compile(
    r"(?<![\w.-])(?:[a-z0-9][a-z0-9-]*\.wd\d+\.myworkdayjobs\.com|wd\d+\.myworkdaysite\.com)"
    r"(?![\w.-])(?:/[^\s\"'<>\\)]*)?", re.I)
_POSTED = re.compile(r"^Posted\s+(?:(Today)|(Yesterday)|(\d+)(\+)?\s+Days?\s+Ago)$", re.I)
_MANY_LOCATIONS = re.compile(r"^(\d+)\s+Locations$", re.I)


@dataclass(frozen=True)
class WorkdayRef:
    """What names one Workday career site. None of it follows from the company
    name: a company may be tenant ``acme``, site ``Careers``, on ``wd5``."""

    tenant: str
    data_center: str     # wd1, wd5, wd503, ...
    site: str

    def __str__(self) -> str:
        return f"{self.tenant}/{self.data_center}/{self.site}"

    @property
    def host(self) -> str:
        return f"{self.tenant.replace('_', '-')}.{self.data_center}.myworkdayjobs.com"

    @property
    def api(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}"

    def job_url(self, external_path: str) -> str:
        return f"https://{self.host}/{self.site}{external_path}"


def parse_workday_url(url: str) -> WorkdayRef | None:
    """The board a Workday URL belongs to, or None.

    Accepts a career site's front page, a job page, a login or apply page, an
    API URL, with or without a locale, on either hostname form:

        https://{tenant}.{wdN}.myworkdayjobs.com/[en-US/]{site}/...
        https://{wdN}.myworkdaysite.com/[en-US/]recruiting/{tenant}/{site}/...

    Only the hostname decides whether a URL is Workday's. A Workday URL that
    names no site (the bare hostname) gives None too.
    """
    text = (url or "").strip()
    try:
        parts = urlsplit(text if "://" in text else "//" + text.lstrip("/"))
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return None
    segments = [s for s in parts.path.split("/") if s]
    jobs, site_host = _JOBS_HOST.match(host), _SITE_HOST.match(host)
    if not jobs and not site_host:
        return None
    if segments[:2] == ["wday", "cxs"]:
        segments = segments[2:]                       # /wday/cxs/{tenant}/{site}/...
    else:
        if segments and _LOCALE.match(segments[0]):
            segments = segments[1:]
        if site_host:
            if not segments or segments[0].lower() != "recruiting":
                return None
            segments = segments[1:]                   # /recruiting/{tenant}/{site}/...
        else:
            segments = [jobs.group(1)] + segments     # /{site}/...
    if len(segments) < 2:
        return None
    tenant, site = segments[0], segments[1]
    if not re.fullmatch(_LABEL, tenant) or not re.fullmatch(_SITE, site) or site.lower() in ("wday", "job"):
        return None
    data_center = (jobs.group(2) if jobs else site_host.group(1)).lower()
    return WorkdayRef(tenant.lower(), data_center, site)


def is_workday_url(url: str) -> bool:
    """Whether the URL's hostname is a Workday career-site host."""
    try:
        host = (urlsplit(url if "://" in url else "//" + url).hostname or "").rstrip(".")
    except ValueError:
        return False
    return bool(_JOBS_HOST.match(host) or _SITE_HOST.match(host))


def parse_ref(ref: str) -> WorkdayRef:
    """A stored reference (``tenant/wdN/site``) or any Workday URL. Raises ``ValueError``."""
    text = (ref or "").strip()
    match = _REF.match(text)
    if match:
        return WorkdayRef(match.group(1).lower(), match.group(2).lower(), match.group(3))
    parsed = parse_workday_url(text)
    if parsed is None:
        raise ValueError(
            f"{text!r} is not a Workday board: expected TENANT/wdN/SITE (e.g. acme/wd5/Careers) "
            "or a URL on myworkdayjobs.com or myworkdaysite.com that names the site")
    return parsed


def find_refs(text: str) -> tuple[list[WorkdayRef], str]:
    """(boards named by the Workday URLs in ``text``, the first Workday URL
    that names no board, or "")."""
    refs: dict[WorkdayRef, None] = {}
    bare = ""
    for match in WORKDAY_URL.finditer(text):
        url = "https://" + match.group(0).rstrip(".,;")
        ref = parse_workday_url(url)
        if ref is not None:
            # The same site linked with different capitalization is one board.
            if not any(r.tenant == ref.tenant and r.site.lower() == ref.site.lower() for r in refs):
                refs[ref] = None
        elif not bare:
            bare = url
    return list(refs), bare


def posted_date(text: object, today: date) -> str:
    """The date behind "Posted 5 Days Ago", or "" when none can be derived
    ("Posted 30+ Days Ago", no text, or wording not recognized)."""
    match = _POSTED.match(str(text or "").strip())
    if not match:
        return ""
    if match.group(1):
        days = 0
    elif match.group(2):
        days = 1
    elif match.group(4):
        return ""
    else:
        days = int(match.group(3))
    return (today - timedelta(days=days)).isoformat()


def _workplace(value: object) -> str:
    text = str(value or "").lower()
    if "remote" in text:
        return "remote"
    if "hybrid" in text:
        return "hybrid"
    if "site" in text or "office" in text:
        return "onsite"
    return ""


def _list_location(item: dict) -> str:
    """The list's location text. When it only says "3 Locations", or says
    nothing, the first location is read from the posting's path."""
    text = collapse_whitespace(item.get("locationsText") or "")
    many = _MANY_LOCATIONS.match(text)
    if text and not many:
        return text
    segments = [s for s in (item.get("externalPath") or "").split("/") if s]
    primary = ""
    if len(segments) >= 3 and segments[0] == "job":
        primary = collapse_whitespace(segments[1].replace("-", " "))
    if many and primary:
        return f"{primary} (+{int(many.group(1)) - 1} more)"
    return primary or text


def _iso_date(value: object) -> str:
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def _flat_facets(facets: object) -> list[dict]:
    """Facets with countable values; groups (locations inside a "main group") opened up."""
    flat = []
    for facet in facets if isinstance(facets, list) else []:
        values = facet.get("values") if isinstance(facet, dict) else None
        if not isinstance(values, list) or not values:
            continue
        if isinstance(values[0], dict) and "values" in values[0]:
            flat.extend(_flat_facets(values))
        elif facet.get("facetParameter"):
            flat.append(facet)
    return flat


def split_facet(facets: object, applied: dict, cap: int) -> tuple[str, list[str]] | None:
    """The facet to split a capped search by: (parameter, value ids), or None.

    Prefers a facet whose every value is under the cap, then the one that
    covers the most postings, then the one with the fewest values.
    """
    best = None
    for facet in _flat_facets(facets):
        param = facet["facetParameter"]
        values = [(v.get("id"), v.get("count") or 0) for v in facet["values"]
                  if isinstance(v, dict) and v.get("id") and (v.get("count") or 0) > 0]
        if param in applied or not 2 <= len(values) <= MAX_SPLIT_VALUES:
            continue
        counts = [count for _, count in values]
        key = (max(counts) < cap, sum(counts), -len(values))
        if best is None or key > best[0]:
            best = (key, param, [value_id for value_id, _ in values])
    return (best[1], best[2]) if best else None


class _BudgetSpent(Exception):
    """This board's fetch has used its time or its requests."""


class _NoTenant(BoardNotFound):
    """HTTP 422: the data center has no tenant by that name."""


class WorkdayAdapter(SourceAdapter):
    """One Workday career site per reference, ``tenant/wdN/site``.

    The list of roles takes one request per 20 roles. Descriptions take one
    request per role and are fetched only for roles the ``FetchContext`` asks
    for: those whose title it wants and those marked saved or applied, and
    only when none is stored yet. Every other role comes back with
    ``description == ""``.

    Requests are at least ``min_interval`` apart. A fetch that runs out of
    time or requests returns what it has and says so in ``last_stats``
    (``complete`` False, or descriptions left waiting).
    """

    name = "workday"
    uses_context = True
    guessable = False

    def __init__(self, session: requests.Session | None = None, *,
                 min_interval: float = MIN_INTERVAL,
                 board_seconds: float = BOARD_SECONDS,
                 max_requests: int = MAX_REQUESTS,
                 max_descriptions: int = MAX_DESCRIPTIONS,
                 result_cap: int = RESULT_CAP,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], date] | None = None) -> None:
        super().__init__(session)
        self.min_interval = min_interval
        self.board_seconds = board_seconds
        self.max_requests = max_requests
        self.max_descriptions = max_descriptions
        self.result_cap = result_cap
        self._sleep = sleep
        self._clock = clock
        self._today = today or (lambda: datetime.now(timezone.utc).date())
        self._last_request: float | None = None
        self._deadline = float("inf")
        self._tenants: dict[tuple[str, str], str] = {}   # (host, site) -> tenant that answered
        self.last_stats = FetchStats()

    # -- references

    @classmethod
    def canonical_ref(cls, ref: str) -> str:
        return str(parse_ref(ref))

    def _ref(self, ref: str) -> WorkdayRef:
        try:
            parsed = parse_ref(ref)
        except ValueError as exc:
            raise SourceError(f"{self.name}: {exc}") from None
        tenant = self._tenants.get((parsed.host, parsed.site.lower()))
        return replace(parsed, tenant=tenant) if tenant else parsed

    # -- requests

    def _send(self, method: str, url: str, ref: WorkdayRef, **kwargs) -> requests.Response:
        """One paced request, retried once when the server says it is busy."""
        stats = self.last_stats
        for attempt in (1, 2):
            if stats.requests >= self.max_requests:
                raise _BudgetSpent(f"used its {self.max_requests} requests")
            if self._clock() >= self._deadline:
                raise _BudgetSpent(f"ran for its {self.board_seconds:g} seconds")
            if self._last_request is not None:
                wait = self.min_interval - (self._clock() - self._last_request)
                if wait > 0:
                    self._sleep(wait)
            stats.requests += 1
            try:
                resp = self.session.request(method, url, timeout=DEFAULT_TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                self._last_request = self._clock()
                if attempt == 1:
                    self._sleep(RETRY_WAIT)
                    continue
                raise SourceError(f"{self.name}: request for {str(ref)!r} failed: {exc}") from exc
            self._last_request = self._clock()
            if resp.status_code in RETRY_STATUSES and attempt == 1:
                try:
                    wait = float(resp.headers.get("Retry-After") or RETRY_WAIT)
                except (TypeError, ValueError):
                    wait = RETRY_WAIT
                self._sleep(min(max(wait, self.min_interval), MAX_RETRY_WAIT))
                continue
            return resp
        raise AssertionError("unreachable")

    def _check_robots(self, ref: WorkdayRef) -> None:
        """Refuse the board when its robots.txt disallows the endpoint we read.

        One request per fetch. A missing file allows everything; a server
        error disallows everything, as RFC 9309 asks.
        """
        resp = self._send("GET", f"https://{ref.host}/robots.txt", ref, headers={"Accept": "*/*"})
        if resp.status_code >= 500:
            raise SourceError(f"{self.name}: robots.txt for {ref.host} answered HTTP {resp.status_code}")
        if resp.status_code != 200:
            return
        rules = RobotFileParser()
        rules.parse((resp.text or "").splitlines())
        path = f"{ref.api}/jobs"
        if not rules.can_fetch(USER_AGENT, path):
            raise SourceError(f"{self.name}: robots.txt on {ref.host} disallows {urlsplit(path).path}")

    def _json(self, resp: requests.Response, ref: WorkdayRef) -> dict:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SourceError(f"{self.name}: non-JSON response for {str(ref)!r}") from exc
        if not isinstance(payload, dict):
            raise SourceError(f"{self.name}: unexpected response shape for {str(ref)!r}")
        return payload

    def _post_page(self, ref: WorkdayRef, applied: dict, offset: int) -> requests.Response:
        return self._send(
            "POST", f"{ref.api}/jobs", ref, headers={"Accept-Language": "en-US"},
            json={"appliedFacets": applied, "limit": PAGE_SIZE, "offset": offset, "searchText": ""})

    def _page(self, ref: WorkdayRef, applied: dict, offset: int) -> dict:
        resp = self._post_page(ref, applied, offset)
        if resp.status_code == 404:
            raise BoardNotFound(f"{self.name}: no career site {ref.site!r} on {ref.host}")
        if resp.status_code == 422:
            raise _NoTenant(
                f"{self.name}: no tenant {ref.tenant!r} on data center {ref.data_center}")
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: HTTP {resp.status_code} for {str(ref)!r}")
        payload = self._json(resp, ref)
        if not isinstance(payload.get("jobPostings"), list):
            raise SourceError(f"{self.name}: unexpected response shape for {str(ref)!r}")
        return payload

    def _first_page(self, ref: WorkdayRef) -> tuple[WorkdayRef, dict]:
        """The first page, and the reference that answered: a tenant written
        with hyphens (as in its hostname) is retried with underscores."""
        try:
            return ref, self._page(ref, {}, 0)
        except _NoTenant:
            other = replace(ref, tenant=ref.tenant.replace("-", "_"))
            if other == ref:
                raise
            payload = self._page(other, {}, 0)
            self._tenants[(ref.host, ref.site.lower())] = other.tenant
            return other, payload

    # -- the interface

    def board_url(self, ref: str) -> str:
        """The list endpoint. It takes a POST and returns 20 roles at a time."""
        return f"{self._ref(ref).api}/jobs"

    def fetch_raw(self, ref: str) -> dict:
        """The first page only: ``total``, up to 20 postings and the facets."""
        self.last_stats = FetchStats()
        self._deadline = float("inf")
        return self._first_page(self._ref(ref))[1]

    def items(self, payload: object, ref: str) -> list[dict]:
        if not isinstance(payload, dict) or not isinstance(payload.get("jobPostings", []), list):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        return payload.get("jobPostings") or []

    def identity(self, ref: str) -> BoardIdentity:
        # A Workday board is never found by guessing a slug, so nothing asks.
        return BoardIdentity()

    def confirm(self, ref: str) -> tuple[str, int]:
        """(reference with the tenant that answered, open roles). Two requests.
        The count stops at 2,000, the most a Workday search reports."""
        self.last_stats = FetchStats()
        self._deadline = float("inf")
        parsed = self._ref(ref)
        self._check_robots(parsed)
        answered, payload = self._first_page(parsed)
        return str(answered), int(payload.get("total") or 0)

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        """Normalize the postings of one list page. No descriptions: the list has none."""
        parsed = self._ref(ref)
        today = self._today()
        roles = (self._role(parsed, item, company, today) for item in self.items(payload, ref))
        return [role for role in roles if role is not None]

    def _role(self, ref: WorkdayRef, item: dict, company: str | None, today: date) -> Role | None:
        path = item.get("externalPath") or ""
        # The last path segment ("Sr-Product-Manager_REF090064W-1") is the
        # posting's anchor: Workday looks a posting up by it, and it keeps its
        # first wording when the title is edited later.
        anchor = path.rstrip("/").rsplit("/", 1)[-1]
        if not anchor or not path.startswith("/"):
            return None
        location = _list_location(item)
        return Role(
            source=self.name,
            source_job_id=f"{ref.tenant.replace('_', '-')}/{ref.site.lower()}/{anchor}",
            company=company or ref.tenant,
            title=collapse_whitespace(item.get("title") or ""),
            department="",   # the list carries none
            location=location,
            workplace_type=_workplace(item.get("remoteType"))
                           or ("remote" if "remote" in location.lower() else ""),
            url=ref.job_url(path),
            date_posted=posted_date(item.get("postedOn"), today),
            compensation_text="",
            description="",
        )

    def fetch(self, ref: str, *, company: str | None = None,
              context: FetchContext | None = None) -> list[Role]:
        context = context or FetchContext()
        self.last_stats = stats = FetchStats()
        self._deadline = self._clock() + self.board_seconds
        parsed = self._ref(ref)
        postings: dict[str, dict] = {}       # externalPath -> item, in listing order
        try:
            self._check_robots(parsed)
            parsed, first = self._first_page(parsed)
            self._collect(parsed, {}, first, postings, 0)
        except _BudgetSpent as exc:
            if not postings:
                raise SourceError(f"{self.name}: {str(parsed)!r} {exc} before any role was read") from None
            stats.complete = False
            stats.notes.append(f"stopped early: it {exc}; roles not reached are left as they were")

        today = self._today()
        roles: list[Role] = []
        paths: dict[str, str] = {}
        for path, item in postings.items():
            role = self._role(parsed, item, company, today)
            if role is None or role.source_job_id in paths:
                continue
            known = context.known.get(role.source_job_id)
            if known is not None:
                # What an earlier fetch stored is as good or better: an exact
                # date where the list now says "30+ days ago", every location
                # where the list says "3 Locations".
                role = replace(
                    role,
                    date_posted=known.date_posted or role.date_posted,
                    location=(known.location if known.has_description and known.location
                              else role.location),
                    workplace_type=role.workplace_type or known.workplace_type)
            paths[role.source_job_id] = path
            roles.append(role)
        return self._with_descriptions(parsed, roles, paths, context)

    # -- listing

    def _collect(self, ref: WorkdayRef, applied: dict, first: dict,
                 out: dict[str, dict], depth: int) -> None:
        """Read every posting of one search into ``out``, splitting it by a
        facet when it is at the cap."""
        stats = self.last_stats
        self._add(first, out)
        total = int(first.get("total") or 0)
        if total >= self.result_cap:
            split = split_facet(first.get("facets"), applied, self.result_cap) \
                if depth < MAX_SPLIT_DEPTH else None
            if split is not None:
                param, value_ids = split
                before = len(out)
                if depth == 0:
                    stats.notes.append(
                        f"more than {self.result_cap} roles: read in {len(value_ids)} parts, by {param}")
                for value_id in value_ids:
                    narrowed = {**applied, param: [value_id]}
                    self._collect(ref, narrowed, self._page(ref, narrowed, 0), out, depth + 1)
                if len(out) - before + PAGE_SIZE < self.result_cap:
                    # The parts add up to less than the search said it had.
                    stats.complete = False
                    stats.notes.append(f"the parts by {param} do not cover the board")
                return
            stats.complete = False
            stats.notes.append(
                f"a search is capped at {self.result_cap} roles and nothing splits it further; "
                "roles beyond the cap were not read")
            total = self.result_cap
        offset = PAGE_SIZE
        # Never ask for an offset at or past the total: the answer would be
        # the first page again.
        while offset < total:
            items = self._page(ref, applied, offset)["jobPostings"]
            self._add({"jobPostings": items}, out)
            if len(items) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

    @staticmethod
    def _add(payload: dict, out: dict[str, dict]) -> None:
        for item in payload.get("jobPostings") or []:
            if isinstance(item, dict) and item.get("externalPath"):
                out.setdefault(item["externalPath"], item)

    # -- descriptions

    def _with_descriptions(self, ref: WorkdayRef, roles: list[Role], paths: dict[str, str],
                           context: FetchContext) -> list[Role]:
        stats = self.last_stats
        marked, wanted = [], []
        for index, role in enumerate(roles):
            known = context.known.get(role.source_job_id)
            if known is not None and known.has_description:
                continue
            if known is not None and known.keep:
                marked.append(index)
            elif context.wants_description(role.title):
                wanted.append(index)
        queue = marked + wanted          # marked roles first, then newest first
        failures = done = 0
        for position, index in enumerate(queue):
            if done >= self.max_descriptions or failures >= DETAIL_FAILURES_TO_STOP:
                stats.descriptions_waiting = len(queue) - position
                break
            role = roles[index]
            try:
                resp = self._send("GET", ref.api + paths[role.source_job_id], ref,
                                  headers={"Accept-Language": "en-US"})
            except _BudgetSpent:
                stats.descriptions_waiting = len(queue) - position
                break
            except SourceError:
                failures += 1
                continue
            done += 1
            if resp.status_code != 200:
                # 404: the posting closed between the list and this request.
                failures = failures + 1 if resp.status_code != 404 else failures
                continue
            try:
                info = self._json(resp, ref).get("jobPostingInfo")
            except SourceError:
                failures += 1
                continue
            if not isinstance(info, dict):
                continue
            failures = 0
            description = strip_html(info.get("jobDescription"))
            if description:
                stats.descriptions_fetched += 1
            places = [collapse_whitespace(str(p)) for p in
                      [info.get("location"), *(info.get("additionalLocations") or [])] if p]
            location = "; ".join(dict.fromkeys(places)) or role.location
            roles[index] = replace(
                role,
                description=description,
                date_posted=_iso_date(info.get("startDate")) or role.date_posted,
                location=location,
                workplace_type=_workplace(info.get("remoteType")) or role.workplace_type
                               or ("remote" if "remote" in location.lower() else ""))
        if stats.descriptions_waiting:
            stats.notes.append(
                f"{stats.descriptions_waiting} description(s) left for the next run")
        return roles
