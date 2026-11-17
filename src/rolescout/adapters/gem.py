"""Gem job boards (the job board API behind jobs.gem.com).

Observed on live boards in November 2026; ``tests/fixtures/gem_*.json`` hold
recorded responses.

- ``GET https://api.gem.com/job_board/v0/{board}/job_posts/`` returns every
  open posting of a board in one response, as a JSON list, with the
  description (HTML and plain text), offices, departments, location type and
  the date it was first published. No key, no paging.
- ``{board}`` is the first path segment of the board's page,
  ``https://jobs.gem.com/{board}``. A job's page is
  ``https://jobs.gem.com/{board}/{id}``.
- An unknown board answers 404 with a JSON message.
- ``api.gem.com/robots.txt`` answers 403 and ``jobs.gem.com/robots.txt`` 404:
  neither host publishes rules.
"""

import re
from urllib.parse import urlsplit

from rolescout.adapters.base import BoardIdentity, FetchContext, FetchStats, SourceAdapter, SourceError
from rolescout.models import Role
from rolescout.text import collapse_whitespace, strip_html

_BOARD = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_REF = re.compile(rf"^{_BOARD}$")
# A Gem URL inside a page. The host part is what makes it one.
GEM_URL = re.compile(r"(?<![\w.-])(?:jobs|api)\.gem\.com(?![\w.-])(?:/[^\s\"'<>\\)]*)?", re.I)
# First path segments of jobs.gem.com that are not boards.
_NOT_A_BOARD = {"api", "static", "assets", "embed", "favicon.ico", "robots.txt", "sitemap.xml"}
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def parse_gem_url(url: str) -> str | None:
    """The board a Gem URL belongs to, or None.

    Accepts a board's page, a job page, an embedded page and an API URL:

        https://jobs.gem.com/{board}[/{job id}][?embed=true]
        https://api.gem.com/job_board/v0/{board}/job_posts/

    A page that embeds a board names it in ``window.__gemJobBoardUrl``, as a
    URL of the first form; the embed script itself (``/gem/embed``) names none.

    Only the hostname decides whether a URL is Gem's.
    """
    text = (url or "").strip()
    try:
        parts = urlsplit(text if "://" in text else "//" + text.lstrip("/"))
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if host == "api.gem.com":
        if segments[:2] != ["job_board", "v0"]:
            return None
        segments = segments[2:]
    elif host != "jobs.gem.com":
        return None
    if not segments or not _REF.match(segments[0]) or segments[0].lower() in _NOT_A_BOARD:
        return None
    if segments[1:2] == ["embed"]:
        return None      # jobs.gem.com/gem/embed is the embed script every customer page loads
    return segments[0].lower()


def parse_ref(ref: str) -> str:
    """A stored reference (the board name) or any Gem URL. Raises ``ValueError``."""
    text = (ref or "").strip()
    if _REF.match(text):
        return text.lower()
    parsed = parse_gem_url(text)
    if parsed is None:
        raise ValueError(f"{text!r} is not a Gem board: expected the board name (e.g. acme) "
                         "or a URL on jobs.gem.com that names the board")
    return parsed


def find_refs(text: str) -> list[str]:
    """The boards named by the Gem URLs in ``text``, in order of appearance."""
    refs: dict[str, None] = {}
    for match in GEM_URL.finditer(text):
        ref = parse_gem_url("https://" + match.group(0).rstrip(".,;"))
        if ref is not None:
            refs.setdefault(ref)
    return list(refs)


def _workplace(value: object) -> str:
    text = str(value or "").lower()
    if "remote" in text:
        return "remote"
    if "hybrid" in text:
        return "hybrid"
    if "office" in text or "site" in text:
        return "onsite"
    return ""


def _location(item: dict) -> str:
    """Every office's place, or the posting's own location when it has no office."""
    places = []
    for office in item.get("offices") or []:
        if isinstance(office, dict):
            place = (office.get("location") or {}).get("name") or office.get("name") or ""
            places.append(collapse_whitespace(str(place)))
    own = collapse_whitespace(str((item.get("location") or {}).get("name") or ""))
    return "; ".join(dict.fromkeys(p for p in places if p)) or own


class GemAdapter(SourceAdapter):
    """One Gem board per reference, the board name in its URL. One request
    returns every role with its description."""

    name = "gem"
    guessable = False
    guess_when_named = True

    @classmethod
    def canonical_ref(cls, ref: str) -> str:
        return parse_ref(ref)

    def _ref(self, ref: str) -> str:
        try:
            return parse_ref(ref)
        except ValueError as exc:
            raise SourceError(f"{self.name}: {exc}") from None

    def board_url(self, ref: str) -> str:
        return f"https://api.gem.com/job_board/v0/{self._ref(ref)}/job_posts/"

    def fetch_raw(self, ref: str) -> object:
        url = self.board_url(ref)
        self._check_robots(url, ref)
        if self.last_stats is not None:
            self.last_stats.requests += 1
        return self._get_json(url, ref)

    def fetch(self, ref: str, *, company: str | None = None,
              context: FetchContext | None = None) -> list[Role]:
        self.last_stats = FetchStats()
        return self.parse(self.fetch_raw(ref), ref, company=company)

    def confirm(self, ref: str) -> tuple[str, int]:
        self.last_stats = FetchStats()
        return self._ref(ref), len(self.items(self.fetch_raw(ref), ref))

    def items(self, payload: object, ref: str) -> list[dict]:
        if not isinstance(payload, list):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        return [item for item in payload if isinstance(item, dict)]

    def identity(self, ref: str) -> BoardIdentity:
        # The API names no company; the board's page title does ("Acme Careers - Gem").
        page = self._get_text(f"https://jobs.gem.com/{self._ref(ref)}", ref)
        match = _TITLE.search(page)
        title = collapse_whitespace(strip_html(match.group(1))) if match else ""
        return BoardIdentity(name=re.sub(r"\s+Careers\s*(?:[-|]\s*Gem)?$|\s*[-|]\s*Gem$", "", title, flags=re.I))

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        board = self._ref(ref)
        roles = []
        for item in self.items(payload, ref):
            job_id = str(item.get("id") or "")
            if not job_id:
                continue
            departments = [d.get("name") or "" for d in item.get("departments") or [] if isinstance(d, dict)]
            location = _location(item)
            roles.append(Role(
                source=self.name,
                source_job_id=job_id,
                company=company or board,
                title=collapse_whitespace(item.get("title") or ""),
                department=next((d for d in departments if d), ""),
                location=location,
                workplace_type=_workplace(item.get("location_type"))
                               or ("remote" if "remote" in location.lower() else ""),
                url=item.get("absolute_url") or f"https://jobs.gem.com/{board}/{job_id}",
                date_posted=str(item.get("first_published_at") or item.get("created_at") or "")[:10],
                compensation_text="",   # the feed has no pay field; pay, if any, is in the description
                description=collapse_whitespace(item.get("content_plain") or "") or strip_html(item.get("content")),
            ))
        return roles
