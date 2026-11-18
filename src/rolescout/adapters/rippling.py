"""Rippling job boards (ats.rippling.com).

Rippling documents no public API. What is here was observed on live boards in
November 2026; ``tests/fixtures/rippling_*.json`` hold recorded responses.

- ``GET https://ats.rippling.com/api/v2/board/{board}/jobs?page=0&pageSize=1000``
  is what a board's own page loads. It lists every open job with its id,
  title, URL, department and location, and says how many pages there are.
  Without ``pageSize`` a page holds 20. It has no description and no date.
- A job open in several places is listed once per place, with the same id.
- ``GET https://ats.rippling.com/{board}/jobs/{id}`` is the job's page. Its
  embedded page data (``__NEXT_DATA__``) holds the description, the date the
  posting was created and any pay range. That is one request per role, so it
  is made only for the roles the caller wants a description for.
- ``GET https://ats.rippling.com/{board}/jobs`` is the board's page; its page
  data names the company. A company that shows its jobs on its own site has
  the board's page redirect there, while the list and the job pages still
  answer.
- An unknown board answers 404 with a JSON message.
- robots.txt disallows only ``/internal/``.
"""

import json
import re
from dataclasses import replace
from urllib.parse import urlsplit

import requests

from rolescout.adapters.base import (
    BoardIdentity, BoardNotFound, FetchContext, FetchStats, SourceAdapter, SourceError,
)
from rolescout.http import DEFAULT_TIMEOUT
from rolescout.models import Role
from rolescout.text import collapse_whitespace, strip_html

HOST = "ats.rippling.com"
PAGE_SIZE = 1000
MAX_PAGES = 20                 # 20,000 list entries: far beyond any board seen
MAX_DESCRIPTIONS = 100         # descriptions fetched per board per fetch; the rest wait
DETAIL_FAILURES_TO_STOP = 3    # consecutive failed job pages

_BOARD = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_REF = re.compile(rf"^{_BOARD}$")
_LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
# A Rippling board URL inside a page. The host part is what makes it one.
RIPPLING_URL = re.compile(r"(?<![\w.-])ats\.rippling\.com(?![\w.-])(?:/[^\s\"'<>\\)]*)?", re.I)
# First path segments that are not boards.
_NOT_A_BOARD = {"api", "embed", "internal", "_next", "static", "assets", "favicon.ico", "robots.txt"}
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_WORKPLACE = {"REMOTE": "remote", "HYBRID": "hybrid", "ON_SITE": "onsite", "ONSITE": "onsite"}
_FREQUENCY = {"YEAR": "per year", "HOUR": "per hour", "MONTH": "per month", "WEEK": "per week", "DAY": "per day"}


def parse_rippling_url(url: str) -> str | None:
    """The board a Rippling URL belongs to, or None.

    Accepts a board's page, a job page, an embedded board and the API URL,
    with or without a locale:

        https://ats.rippling.com/[en-GB/]{board}/jobs[/{job id}]
        https://ats.rippling.com/[en-GB/]embed/{board}/jobs
        https://ats.rippling.com/api/v2/board/{board}/jobs

    Only the hostname decides whether a URL is Rippling's.
    """
    text = (url or "").strip()
    try:
        parts = urlsplit(text if "://" in text else "//" + text.lstrip("/"))
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if host != HOST:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if segments[:3] == ["api", "v2", "board"]:
        segments = segments[3:]
    else:
        if segments and _LOCALE.match(segments[0]):
            segments = segments[1:]
        if segments and segments[0] == "embed":
            segments = segments[1:]
    if not segments or not _REF.match(segments[0]) or segments[0].lower() in _NOT_A_BOARD:
        return None
    return segments[0].lower()


def parse_ref(ref: str) -> str:
    """A stored reference (the board name) or any Rippling board URL. Raises ``ValueError``."""
    text = (ref or "").strip()
    if _REF.match(text):
        return text.lower()
    parsed = parse_rippling_url(text)
    if parsed is None:
        raise ValueError(f"{text!r} is not a Rippling board: expected the board name (e.g. acme) "
                         "or a URL on ats.rippling.com that names the board")
    return parsed


def find_refs(text: str) -> list[str]:
    """The boards named by the Rippling URLs in ``text``, in order of appearance."""
    refs: dict[str, None] = {}
    for match in RIPPLING_URL.finditer(text):
        ref = parse_rippling_url("https://" + match.group(0).rstrip(".,;"))
        if ref is not None:
            refs.setdefault(ref)
    return list(refs)


def page_data(page: str) -> dict:
    """What a board or job page embeds for its own rendering, or {}."""
    match = _NEXT_DATA.search(page or "")
    try:
        data = json.loads(match.group(1)) if match else {}
        found = data["props"]["pageProps"]["apiData"]
    except (ValueError, KeyError, TypeError):
        return {}
    return found if isinstance(found, dict) else {}


def _pay(ranges: object) -> str:
    """"USD 185,000 - 240,000 per year (US)" for each pay range a posting gives."""
    out = []
    for item in ranges if isinstance(ranges, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            low, high = float(item.get("rangeStart") or 0), float(item.get("rangeEnd") or 0)
        except (TypeError, ValueError):
            continue
        if not low and not high:
            continue
        amount = f"{low:,.0f} - {high:,.0f}" if low and high and low != high else f"{(high or low):,.0f}"
        text = " ".join(p for p in (str(item.get("currency") or ""), amount,
                                    _FREQUENCY.get(str(item.get("frequency") or "").upper(), "")) if p)
        where = collapse_whitespace(str(item.get("location") or ""))
        out.append(text + (f" ({where})" if where else ""))
    return "; ".join(dict.fromkeys(out))


class RipplingAdapter(SourceAdapter):
    """One Rippling board per reference, the board name in its URL.

    The list of roles takes one request. Descriptions, posted dates and pay
    take one request per role and are fetched only for roles the
    ``FetchContext`` asks for: those whose title it wants and those marked
    saved or applied, and only when none is stored yet. Every other role
    comes back with ``description == ""`` and no date.
    """

    name = "rippling"
    uses_context = True
    guessable = False
    guess_when_named = True

    def __init__(self, session: requests.Session | None = None, *,
                 max_descriptions: int = MAX_DESCRIPTIONS) -> None:
        super().__init__(session)
        self.max_descriptions = max_descriptions
        self.last_stats = FetchStats()

    @classmethod
    def canonical_ref(cls, ref: str) -> str:
        return parse_ref(ref)

    def _ref(self, ref: str) -> str:
        try:
            return parse_ref(ref)
        except ValueError as exc:
            raise SourceError(f"{self.name}: {exc}") from None

    def board_url(self, ref: str) -> str:
        return self._page_url(self._ref(ref), 0)

    @staticmethod
    def _page_url(board: str, page: int) -> str:
        return f"https://{HOST}/api/v2/board/{board}/jobs?page={page}&pageSize={PAGE_SIZE}"

    def fetch_raw(self, ref: str) -> dict:
        """Every list entry of the board, all pages gathered under ``items``."""
        board = self._ref(ref)
        stats = self.last_stats
        self._check_robots(self._page_url(board, 0), ref)
        stats.requests += 1
        payload = self._get_json(self._page_url(board, 0), ref)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        try:
            pages = int(payload.get("totalPages") or 1)
        except (TypeError, ValueError):
            pages = 1
        for page in range(1, min(pages, MAX_PAGES)):
            stats.requests += 1
            more = self._get_json(self._page_url(board, page), ref)
            if not isinstance(more, dict) or not isinstance(more.get("items"), list):
                raise SourceError(f"{self.name}: unexpected response shape for {ref!r} (page {page})")
            payload["items"] += more["items"]
        try:
            listed = int(payload.get("totalItems"))
        except (TypeError, ValueError):
            listed = None
        if listed is not None:
            stats.count(len(payload["items"]), listed)
        if pages > MAX_PAGES:
            stats.complete = False
            stats.notes.append(f"the board has {pages} pages; only the first {MAX_PAGES} were read")
        return payload

    def items(self, payload: object, ref: str) -> list[dict]:
        """One object per job: the entries that share an id, with every
        location they name gathered under ``locations``."""
        if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        jobs: dict[str, dict] = {}
        for entry in payload.get("items") or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            job = jobs.setdefault(str(entry["id"]), {**entry, "locations": []})
            job["locations"] += [loc for loc in entry.get("locations") or []
                                 if isinstance(loc, dict) and loc not in job["locations"]]
        return list(jobs.values())

    def confirm(self, ref: str) -> tuple[str, int]:
        self.last_stats = FetchStats()
        return self._ref(ref), len(self.items(self.fetch_raw(ref), ref))

    def identity(self, ref: str) -> BoardIdentity:
        board = self._ref(ref)
        info = page_data(self._get_text(f"https://{HOST}/{board}/jobs", ref)).get("jobBoard") or {}
        site = str(info.get("boardURL") or "")
        # A board shown on the company's own site says where; otherwise the
        # address is Rippling's and says nothing about the company.
        own = site if (urlsplit(site).hostname or "").lower() not in ("", HOST) else ""
        return BoardIdentity(name=str(info.get("companyName") or "").strip(), website=own)

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        """Normalize the list. No descriptions or dates: the list has none."""
        board = self._ref(ref)
        roles = []
        for item in self.items(payload, ref):
            job_id = str(item["id"])
            kinds = {_WORKPLACE.get(str(loc.get("workplaceType") or "").upper(), "") for loc in item["locations"]}
            mixed = len(kinds) > 1
            places = []
            for loc in item["locations"]:
                name = collapse_whitespace(str(loc.get("name") or ""))
                kind = _WORKPLACE.get(str(loc.get("workplaceType") or "").upper(), "")
                # When one job is remote in one place and not in another, each place says which.
                if name and mixed and kind == "remote" and "remote" not in name.lower():
                    name = f"{name} (Remote)"
                if name:
                    places.append(name)
            department = item.get("department") or {}
            roles.append(Role(
                source=self.name,
                source_job_id=job_id,
                company=company or board,
                title=collapse_whitespace(item.get("name") or ""),
                department=collapse_whitespace(str(department.get("name") or "")) if isinstance(department, dict) else "",
                location="; ".join(dict.fromkeys(places)),
                workplace_type=next(iter(kinds)) if len(kinds) == 1 else "",
                url=item.get("url") or f"https://{HOST}/{board}/jobs/{job_id}",
                date_posted="",
                compensation_text="",
                description="",
            ))
        return roles

    def fetch(self, ref: str, *, company: str | None = None,
              context: FetchContext | None = None) -> list[Role]:
        context = context or FetchContext()
        self.last_stats = stats = FetchStats()
        board = self._ref(ref)
        roles = self.parse(self.fetch_raw(ref), ref, company=company)
        marked, wanted = [], []
        for index, role in enumerate(roles):
            known = context.known.get(role.source_job_id)
            if known is not None:
                # The list has no date; keep the one an earlier fetch read.
                roles[index] = role = replace(role, date_posted=known.date_posted)
                if known.has_description:
                    continue
                if known.keep:
                    marked.append(index)
                    continue
            if context.wants_description(role.title):
                wanted.append(index)
        queue = marked + wanted
        failures = done = 0
        for position, index in enumerate(queue):
            if done >= self.max_descriptions or failures >= DETAIL_FAILURES_TO_STOP:
                stats.descriptions_waiting = len(queue) - position
                break
            role = roles[index]
            stats.requests += 1
            done += 1
            try:
                resp = self.session.get(f"https://{HOST}/{board}/jobs/{role.source_job_id}",
                                        timeout=DEFAULT_TIMEOUT, headers={"Accept": "text/html"})
            except requests.RequestException:
                failures += 1
                continue
            if resp.status_code != 200:
                # 404: the posting closed between the list and this request.
                failures = failures + 1 if resp.status_code != 404 else failures
                continue
            post = page_data(resp.text).get("jobPost")
            if not isinstance(post, dict):
                failures += 1
                continue
            failures = 0
            parts = post.get("description") if isinstance(post.get("description"), dict) else {}
            description = collapse_whitespace(" ".join(
                strip_html(parts.get(key)) for key in ("company", "role") if parts.get(key)))
            if description:
                stats.descriptions_fetched += 1
            roles[index] = replace(
                role, description=description,
                date_posted=str(post.get("createdOn") or "")[:10] or role.date_posted,
                compensation_text=_pay(post.get("payRangeDetails")))
        if stats.descriptions_waiting:
            stats.notes.append(f"{stats.descriptions_waiting} description(s) left for the next run")
        return roles
