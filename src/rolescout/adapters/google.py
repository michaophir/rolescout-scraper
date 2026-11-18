"""Google's own career site: the single-company exception named in CLAUDE.md.

The site runs on no shared job system, so no other adapter can read it.
Observed live in November 2026;
``tests/fixtures/google_feed_roles_from_several_employers.xml`` holds a
recorded, shortened feed.

- ``GET https://www.google.com/about/careers/applications/jobs/feed.xml``
  returns every open role in one XML document (about 20 MB, 4 MB compressed).
  No key, no paging.
- Each ``<job>`` has ``jobid``, ``title``, ``url``, ``published``,
  ``employer``, ``categories``, ``locations`` (city, state, country),
  ``remote`` (onsite or remote), ``isHybrid`` and a ``description``.
- ``employer`` is the company in the group that is hiring. Every role
  belongs to the one company the board is mapped to; the employer is kept at
  the front of the role's department.
- robots.txt allows the feed. It disallows the paged search results, which
  this adapter never reads.
- The feed carries every description, at no extra request. They are kept
  the way the sources that charge a request per description keep them: for
  roles whose title the ranking config wants, and for roles marked saved or
  applied. A fetch given no context (``rolescout fetch``) keeps them all.
"""

from dataclasses import replace

from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests

from rolescout.adapters.base import BoardIdentity, BoardNotFound, FetchContext, FetchStats, SourceAdapter, SourceError
from rolescout.http import DEFAULT_TIMEOUT
from rolescout.models import Role
from rolescout.text import collapse_whitespace, strip_html

FEED_URL = "https://www.google.com/about/careers/applications/jobs/feed.xml"
FEED_TIMEOUT = max(DEFAULT_TIMEOUT, 120)   # one large document
REF = "careers"                            # the one board there is
_HOSTS = {"www.google.com", "google.com", "careers.google.com"}
_FIELDS = ("jobid", "title", "url", "published", "employer", "remote", "isRemote", "isHybrid")


def parse_ref(ref: str) -> str:
    """``careers``, or any URL of the career site. Raises ``ValueError``."""
    text = (ref or "").strip()
    if text.lower() in (REF, "google"):
        return REF
    try:
        parts = urlsplit(text if "://" in text else "//" + text.lstrip("/"))
        host = (parts.hostname or "").lower()
    except ValueError:
        host, parts = "", None
    if host == "careers.google.com" or (host in _HOSTS and parts.path.startswith("/about/careers")):
        return REF
    raise ValueError(f"{text!r} is not the Google career site: the reference is {REF!r}")


def _label(code: str) -> str:
    """SOFTWARE_ENGINEERING -> Software Engineering."""
    return " ".join(word.capitalize() for word in code.replace("_", " ").split())


def _place(location: ElementTree.Element) -> str:
    parts = [collapse_whitespace(location.findtext(tag) or "") for tag in ("city", "state", "country")]
    return ", ".join(part for part in parts if part)


class GoogleAdapter(SourceAdapter):
    name = "google"
    guessable = False      # one company's site: it is mapped by hand, never guessed for others
    uses_context = True    # which descriptions are kept depends on the caller

    @classmethod
    def canonical_ref(cls, ref: str) -> str:
        return parse_ref(ref)

    def board_url(self, ref: str) -> str:
        parse_ref(ref)
        return FEED_URL

    def fetch_raw(self, ref: str) -> list[dict]:
        """The feed's jobs as plain objects."""
        url = self.board_url(ref)
        if self.last_stats is None:
            self.last_stats = FetchStats()
        self._check_robots(url, ref)
        self.last_stats.requests += 1
        try:
            resp = self.session.get(url, timeout=FEED_TIMEOUT, headers={"Accept": "text/xml, application/xml"})
        except requests.RequestException as exc:
            raise SourceError(f"{self.name}: request for the feed failed: {exc}") from exc
        if resp.status_code == 404:
            raise BoardNotFound(f"{self.name}: the feed is no longer at {url}")
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: HTTP {resp.status_code} for the feed")
        # The bytes, not the text: the feed is UTF-8 and says so itself, while
        # an HTTP library may guess otherwise for text/xml.
        body = getattr(resp, "content", None) or (resp.text or "").encode("utf-8")
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise SourceError(f"{self.name}: the feed is not well-formed XML: {exc}") from exc
        jobs = []
        for job in root.iter("job"):
            item = {tag: collapse_whitespace(job.findtext(tag) or "") for tag in _FIELDS}
            item["description"] = job.findtext("description") or ""
            item["categories"] = [c.text.strip() for c in job.iter("category") if (c.text or "").strip()]
            item["places"] = list(dict.fromkeys(p for p in map(_place, job.iter("location")) if p))
            jobs.append(item)
        if root.tag != "jobs" or not jobs:
            # A feed with no roles is a broken feed, not a company that stopped hiring.
            raise SourceError(f"{self.name}: the feed lists no roles; it was not read as an empty board")
        return jobs

    def fetch(self, ref: str, *, company: str | None = None,
              context: FetchContext | None = None) -> list[Role]:
        self.last_stats = stats = FetchStats()
        roles = self.parse(self.fetch_raw(ref), ref, company=company)
        if context is None:
            return roles
        for index, role in enumerate(roles):
            known = context.known.get(role.source_job_id)
            if not ((known is not None and known.keep) or context.wants_description(role.title)):
                roles[index] = replace(role, description="")
            elif role.description and not (known is not None and known.has_description):
                stats.descriptions_fetched += 1
        return roles

    def confirm(self, ref: str) -> tuple[str, int]:
        self.last_stats = FetchStats()
        return REF, len(self.items(self.fetch_raw(ref), ref))

    def items(self, payload: object, ref: str) -> list[dict]:
        if not isinstance(payload, list):
            raise SourceError(f"{self.name}: unexpected feed contents")
        unique: dict[str, dict] = {}
        for item in payload:
            if isinstance(item, dict) and item.get("jobid"):
                unique.setdefault(item["jobid"], item)
        return list(unique.values())

    def identity(self, ref: str) -> BoardIdentity:
        return BoardIdentity(name="Google", website="https://www.google.com")

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        roles = []
        for item in self.items(payload, ref):
            categories = ", ".join(_label(c) for c in item.get("categories") or []
                                   if c != "JOB_CATEGORY_UNSPECIFIED")
            employer = item.get("employer") or ""
            if item.get("isHybrid", "").lower() == "yes":
                workplace = "hybrid"
            elif item.get("remote", "").lower() == "remote" or item.get("isRemote", "").lower() == "yes":
                workplace = "remote"
            else:
                workplace = "onsite" if item.get("remote", "").lower() == "onsite" else ""
            roles.append(Role(
                source=self.name,
                source_job_id=item["jobid"],
                company=company or "Google",
                title=item.get("title") or "",
                # The employer first, so a role at another company in the group says so.
                department=": ".join(part for part in (employer, categories) if part),
                location="; ".join(item.get("places") or []),
                workplace_type=workplace,
                url=item.get("url") or "",
                date_posted=(item.get("published") or "")[:10],
                compensation_text="",
                description=strip_html(item.get("description")),
            ))
        return roles
