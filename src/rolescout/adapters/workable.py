"""Workable job boards (the feed behind Workable's careers-page widget).

Observed on live boards in November 2026; ``tests/fixtures/workable_*.json``
hold recorded responses.

- ``GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true``
  returns the account's name and every published job with its description,
  in one response. No key, no paging.
- ``{account}`` is the name in the board's address
  (``https://apply.workable.com/{account}/``, formerly
  ``https://{account}.workable.com``). The numeric account id that a
  careers-page embed carries (``whr_embed(12345, ...)``) works in its place.
- A job open in several places is listed once per place, with the same
  ``shortcode``; the entries differ only in their location.
- ``telecommuting`` says a job is remote. Hybrid is not marked.
- ``GET https://apply.workable.com/api/v1/accounts/{account}`` gives the
  company's name and website (by name only, not by numeric id).
- An unknown account answers 404. robots.txt allows everything.
"""

import re
from urllib.parse import urlsplit

from rolescout.adapters.base import (
    BoardIdentity, BoardNotFound, FetchContext, FetchStats, SourceAdapter, SourceError,
)
from rolescout.models import Role
from rolescout.text import collapse_whitespace, strip_html

_ACCOUNT = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_REF = re.compile(rf"^{_ACCOUNT}$")
_SHORTCODE = re.compile(r"^[A-Za-z0-9]{6,16}$")
# A Workable URL inside a page: the apply host, the API on the main host, or
# an account's own subdomain.
WORKABLE_URL = re.compile(
    r"(?<![\w.-])(?:[a-z0-9][a-z0-9-]*\.)?workable\.com(?![\w.-])(?:/[^\s\"'<>\\)]*)?", re.I)
# The careers-page embed: whr_embed(12345, {...}) names the account by number.
WORKABLE_EMBED = re.compile(r"\bwhr_embed\s*\(\s*[\"']?(\d{2,12})[\"']?\s*[,)]", re.I)
# Subdomains of workable.com that are Workable's own, not an account's.
_OWN_HOSTS = {"www", "apply", "jobs", "help", "resources", "careers", "api", "developers", "id", "status",
              "blog", "get", "support", "hiring", "engage", "partners", "community", "docs", "cdn", "assets"}
# First path segments of apply.workable.com that are not accounts.
_NOT_AN_ACCOUNT = {"api", "j", "static", "assets", "favicon.ico", "robots.txt", "sitemap.xml"}


def parse_workable_url(url: str) -> str | None:
    """The account a Workable URL belongs to, or None.

    Accepts a board's page, a job page, an application page, an account's
    own subdomain and the API URLs:

        https://apply.workable.com/{account}/[j/{shortcode}/[apply/]]
        https://{account}.workable.com/[jobs/{id}]
        https://apply.workable.com/api/v1/widget/accounts/{account}
        https://apply.workable.com/api/v3/accounts/{account}/jobs
        https://www.workable.com/api/accounts/{account}

    A job link with no account in it (``apply.workable.com/j/{shortcode}``)
    gives None: it names the job, not the board.
    """
    text = (url or "").strip()
    try:
        parts = urlsplit(text if "://" in text else "//" + text.lstrip("/"))
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return None
    if host != "workable.com" and not host.endswith(".workable.com"):
        return None
    segments = [s for s in parts.path.split("/") if s]
    sub = host[:-len(".workable.com")] if host != "workable.com" else ""
    if "accounts" in segments and segments[0] == "api":
        after = segments[segments.index("accounts") + 1:]
        account = after[0] if after else ""
    elif sub == "apply":
        account = segments[0] if segments else ""
        if account.lower() in _NOT_AN_ACCOUNT:
            return None
    elif sub and "." not in sub and sub not in _OWN_HOSTS:
        account = sub
    else:
        return None
    return account.lower() if _REF.match(account) else None


def parse_ref(ref: str) -> str:
    """A stored reference (the account name or number) or any Workable URL. Raises ``ValueError``."""
    text = (ref or "").strip()
    if _REF.match(text):
        return text.lower()
    parsed = parse_workable_url(text)
    if parsed is None:
        raise ValueError(f"{text!r} is not a Workable board: expected the account name (e.g. acme) "
                         "or a URL on apply.workable.com that names the account")
    return parsed


def find_refs(text: str) -> list[str]:
    """The accounts named in ``text``: by Workable URLs first, then by the
    account number of a careers-page embed."""
    refs: dict[str, None] = {}
    for match in WORKABLE_URL.finditer(text):
        ref = parse_workable_url("https://" + match.group(0).rstrip(".,;"))
        if ref is not None:
            refs.setdefault(ref)
    if not refs:
        for match in WORKABLE_EMBED.finditer(text):
            refs.setdefault(match.group(1))
    return list(refs)


def _place(location: dict) -> str:
    parts = [location.get("city"), location.get("region") or location.get("state"), location.get("country")]
    return ", ".join(dict.fromkeys(collapse_whitespace(str(p)) for p in parts if p))


def _places(entry: dict) -> list[str]:
    listed = [_place(loc) for loc in entry.get("locations") or []
              if isinstance(loc, dict) and not loc.get("hidden")]
    return [p for p in listed if p] or [p for p in [_place(entry)] if p]


class WorkableAdapter(SourceAdapter):
    """One Workable account per reference: its name, or its number. One
    request returns every role with its description."""

    name = "workable"
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
        return f"https://apply.workable.com/api/v1/widget/accounts/{self._ref(ref)}?details=true"

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
        """One object per job: the entries that share a shortcode, with every
        place they name gathered under ``places``."""
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs", []), list):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        jobs: dict[str, dict] = {}
        for entry in payload.get("jobs") or []:
            if not isinstance(entry, dict) or not entry.get("shortcode"):
                continue
            job = jobs.setdefault(str(entry["shortcode"]), {**entry, "places": []})
            job["places"] += [p for p in _places(entry) if p not in job["places"]]
        return list(jobs.values())

    def identity(self, ref: str) -> BoardIdentity:
        account = self._ref(ref)
        try:
            info = self._get_json(f"https://apply.workable.com/api/v1/accounts/{account}", ref)
        except BoardNotFound:
            info = None     # a numeric account id is not answered here
        if isinstance(info, dict):
            return BoardIdentity(name=str(info.get("name") or "").strip(), website=str(info.get("url") or "").strip())
        payload = self._get_json(self.board_url(ref), ref)
        return BoardIdentity(name=str(payload.get("name") or "").strip() if isinstance(payload, dict) else "")

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        account = self._ref(ref)
        named = str(payload.get("name") or "").strip() if isinstance(payload, dict) else ""
        roles = []
        for item in self.items(payload, ref):
            code = str(item["shortcode"])
            location = "; ".join(item["places"])
            remote = bool(item.get("telecommuting"))
            roles.append(Role(
                source=self.name,
                source_job_id=code,
                company=company or named or account,
                title=collapse_whitespace(item.get("title") or ""),
                department=collapse_whitespace(str(item.get("department") or "")),
                location=location,
                workplace_type="remote" if remote or "remote" in location.lower() else "",
                url=item.get("url") or item.get("shortlink") or f"https://apply.workable.com/j/{code}",
                date_posted=str(item.get("published_on") or item.get("created_at") or "")[:10],
                compensation_text="",   # the feed has no pay field; pay, if any, is in the description
                description=strip_html(item.get("description")),
            ))
        return roles
