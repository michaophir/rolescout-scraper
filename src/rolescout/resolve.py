"""Find each company's job board once, with evidence, and cache the mapping.

Two kinds of evidence, in order:

1. **Link** (strong). The company's own website or careers page links to a
   board on a supported source, and that board exists. Saved automatically.
2. **Guess** (weak). A board exists at a slug derived from the company name
   or domain. Saved only when something ties the board to the company's own
   domain; a matching name alone is never enough, because namesakes exist.

Anything else is reported and left unmapped. When the careers page points at
a job system we have no adapter for, that system and URL are recorded.
"""

import html
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from rolescout.adapters import ADAPTERS, BoardNotFound, SourceError, get_adapter
from rolescout.companies import CompanyError, add_board
from rolescout.db import transaction, utc_now
from rolescout.http import make_session

PAGE_TIMEOUT = 20
PAGE_MAX_CHARS = 3_000_000
MAX_CAREERS_PAGES = 2          # careers pages followed from the homepage
MAX_LINKED_BOARDS = 2          # more distinct boards than this on one site is suspicious
# A guessed board is confirmed when at least this share of its postings
# reference the company's own domain (job URL, link or email address).
MIN_DOMAIN_SHARE = 0.25
GUESS_SOURCES = ("greenhouse", "lever", "ashby")

# --- what a job-board link looks like ----------------------------------------

_SLUG = r"([A-Za-z0-9][A-Za-z0-9_.%-]*)"
SUPPORTED_LINKS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(
        r"greenhouse\.io/embed/job_(?:board|app)(?:/js)?\?[^\"'\s<>]*?\bfor=([A-Za-z0-9_]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_]+)", re.I)),
    ("greenhouse", re.compile(r"(?<![\w.-])(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_]+)", re.I)),
    ("lever", re.compile(r"(?<![\w.-])jobs\.lever\.co/" + _SLUG, re.I)),
    ("lever", re.compile(r"(?<![\w.-])api\.lever\.co/v0/postings/" + _SLUG, re.I)),
    ("ashby", re.compile(r"(?<![\w.-])jobs\.ashbyhq\.com/" + _SLUG, re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _SLUG, re.I)),
]
NOT_A_SLUG = {"embed", "api", "v1", "jobs", "job", "static", "assets", "js", "css"}

# Job systems we can recognize but have no adapter for. Each pattern matches
# host (and sometimes the first path segment); the rest of the URL is kept.
UNSUPPORTED_SYSTEMS: list[tuple[str, str]] = [
    ("workday", r"[\w.-]+\.myworkday(?:jobs|site)\.com"),
    ("smartrecruiters", r"(?:jobs|careers|api)\.smartrecruiters\.com/[\w-]+"),
    ("workable", r"apply\.workable\.com/[\w-]+|(?!www\.)[\w-]+\.workable\.com"),
    ("rippling", r"ats\.rippling\.com/[\w-]+"),
    ("eightfold", r"(?!www\.)[\w-]+\.eightfold\.ai"),
    ("icims", r"(?!www\.)[\w.-]+\.icims\.com"),
    ("jobvite", r"jobs\.jobvite\.com/[\w-]+"),
    ("bamboohr", r"(?!www\.)[\w-]+\.bamboohr\.com"),
    ("teamtailor", r"(?!www\.)[\w-]+\.teamtailor\.com"),
    ("recruitee", r"(?!www\.)[\w-]+\.recruitee\.com"),
    ("personio", r"[\w-]+\.jobs\.personio\.(?:com|de)"),
    ("gem", r"jobs\.gem\.com/[\w-]+"),
    ("breezy", r"(?!www\.)[\w-]+\.breezy\.hr"),
    ("jazzhr", r"(?!www\.)[\w-]+\.applytojob\.com"),
    ("taleo", r"[\w.-]+\.taleo\.net"),
    ("oracle", r"[\w.-]+\.oraclecloud\.com/hcmUI/CandidateExperience"),
    ("successfactors", r"[\w.-]+\.successfactors\.(?:com|eu)"),
    ("avature", r"(?!www\.)[\w-]+\.avature\.net"),
    ("pinpoint", r"(?!www\.)[\w-]+\.pinpointhq\.com"),
    ("dover", r"app\.dover\.com/[\w-]+/careers|jobs\.dover\.com/[\w-]+"),
    ("greenhouse-eu", r"(?:job-boards|boards)\.eu\.greenhouse\.io/[\w-]+"),
    ("lever-eu", r"jobs\.eu\.lever\.co/[\w.-]+"),
]
UNSUPPORTED_LINKS = [
    (system, re.compile(r"(?<![\w.-])(?:" + pattern + r")[^\s\"'<>\\)]*", re.I))
    for system, pattern in UNSUPPORTED_SYSTEMS
]
CUSTOM = "custom"  # a careers page on the company's own site with no known job system

CAREERS_HREF = re.compile(
    r"career|/jobs?(?:[/?#]|$)|join-?us|work-with-us|open-(?:roles|positions)|openings|we-are-hiring", re.I)
CAREERS_TEXT = re.compile(
    r"^\s*(?:careers?|jobs|join us|join our team|work with us|we(?:'|’)re hiring|open (?:roles|positions)|"
    r"view (?:all )?(?:open )?(?:roles|jobs|positions|openings)|see (?:all )?open (?:roles|positions))\b", re.I)
NOT_CAREERS_HOSTS = re.compile(
    r"(?:^|\.)(?:linkedin|glassdoor|indeed|wellfound|angel|twitter|x|facebook|instagram|youtube|"
    r"github|ycombinator|builtin|levels|tiktok|medium)\.(?:com|co|fyi)$", re.I)
FALLBACK_CAREERS_PATHS = ("/careers", "/jobs")

_TWO_PART_TLDS = {"co", "com", "org", "net", "ac", "gov", "edu"}
_NAME_NOISE = {"inc", "llc", "ltd", "corp", "co", "company", "labs", "lab", "technologies",
               "technology", "tech", "hq", "ai", "the", "jobs", "careers", "group"}


# --- results -----------------------------------------------------------------

@dataclass
class Candidate:
    source: str
    ref: str
    kind: str              # link | guess
    status: str            # confirmed | unconfirmed | rejected
    detail: str            # the evidence, in words
    roles: int | None = None
    saved: bool = False


@dataclass
class Resolution:
    company: str
    outcome: str = "unresolved"   # mapped | needs_review | unsupported | unresolved
    job_system: str | None = None
    job_system_url: str | None = None
    careers_url: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    requests: int = 0

    @property
    def to_save(self) -> list[Candidate]:
        return [c for c in self.candidates if c.status == "confirmed"] \
            if self.outcome == "mapped" else []


# --- small helpers -----------------------------------------------------------

def registrable_domain(url_or_host: str) -> str:
    """example.com from https://www.jobs.example.com/x. Heuristic, no suffix list."""
    text = (url_or_host or "").strip().lower()
    if not text:
        return ""
    host = urlparse(text if "//" in text else "//" + text).hostname or ""
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return ""
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _TWO_PART_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def domain_pattern(domain: str) -> re.Pattern:
    """Matches the domain or any subdomain of it, but not look-alikes
    (ramp.com does not match xramp.com, ramp.com.au or ramp.community)."""
    return re.compile(r"(?<![\w.-])(?:[\w-]+\.)*" + re.escape(domain) + r"(?![\w-]|\.[a-z])", re.I)


def name_key(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return "".join(w for w in words if w not in _NAME_NOISE) or "".join(words)


def names_match(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    return bool(ka and kb) and (ka == kb or ka.startswith(kb) or kb.startswith(ka))


def slug_guesses(name: str, website: str) -> list[str]:
    """Slugs worth trying: the name hyphenated and squashed, and the domain label."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    label = registrable_domain(website).split(".")[0]
    return [s for s in dict.fromkeys([base, base.replace("-", ""), label]) if len(s) >= 2]


def _normalize(text: str) -> str:
    """Undo the escaping that hides URLs inside HTML attributes and inline JSON."""
    text = html.unescape(text)
    return text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")


def scan_for_boards(text: str) -> tuple[dict[tuple[str, str], None], dict[str, str]]:
    """(supported boards as {(source, slug)}, unsupported systems as {system: url})."""
    text = _normalize(text)
    supported: dict[tuple[str, str], None] = {}
    for source, pattern in SUPPORTED_LINKS:
        for match in pattern.finditer(text):
            slug = unquote(match.group(1)).strip(".")
            if source != "ashby":
                slug = slug.lower()
            if slug and slug.lower() not in NOT_A_SLUG:
                supported.setdefault((source, slug))
    unsupported: dict[str, str] = {}
    for system, pattern in UNSUPPORTED_LINKS:
        match = pattern.search(text)
        if match:
            unsupported[system] = "https://" + match.group(0).rstrip(".,;")
    return supported, unsupported


def careers_links(page_html: str, page_url: str, own_domains: set[str]) -> list[str]:
    """Links on a page that look like they lead to the company's careers page."""
    found: dict[str, int] = {}
    soup = BeautifulSoup(page_html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(page_url, href).split("#")[0]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        if NOT_CAREERS_HOSTS.search(parsed.hostname):
            continue
        by_href = bool(CAREERS_HREF.search(parsed.hostname + parsed.path))
        by_text = bool(CAREERS_TEXT.search(anchor.get_text(" ", strip=True)))
        if not (by_href or by_text):
            continue
        own = registrable_domain(url) in own_domains
        # Prefer links on the company's own domain, then ones matching on both.
        score = (2 if own else 0) + by_href + by_text
        if url.rstrip("/") != page_url.rstrip("/"):
            found[url] = max(found.get(url, 0), score)
    return sorted(found, key=lambda u: (-found[u], len(u)))


# --- the resolver ------------------------------------------------------------

class Resolver:
    def __init__(self, session: requests.Session | None = None, delay: float = 1.0) -> None:
        self.session = session or make_session(min_interval=delay)
        self._adapters: dict[str, object] = {}

    def adapter(self, source: str):
        if source not in self._adapters:
            self._adapters[source] = get_adapter(source, self.session)
        return self._adapters[source]

    # -- pages

    def _page(self, url: str, res: Resolution) -> tuple[str, str] | None:
        """(final URL, HTML) or None. Never raises."""
        res.requests += 1
        try:
            resp = self.session.get(
                url, timeout=PAGE_TIMEOUT,
                headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
        except requests.RequestException as exc:
            res.notes.append(f"could not fetch {url}: {type(exc).__name__}")
            return None
        if resp.status_code != 200:
            res.notes.append(f"could not fetch {url}: HTTP {resp.status_code}")
            return None
        return (resp.url or url), (resp.text or "")[:PAGE_MAX_CHARS]

    def _site_evidence(self, website: str, res: Resolution, own_domains: set[str]):
        """Read the homepage and up to two careers pages. Returns
        (supported {(source, slug): found_on}, unsupported {system: (url, found_on)})."""
        supported: dict[tuple[str, str], str] = {}
        unsupported: dict[str, tuple[str, str]] = {}

        def scan(final_url: str, body: str) -> None:
            # The final URL counts too: /careers often redirects to the job system.
            boards, systems = scan_for_boards(final_url + "\n" + body)
            for key in boards:
                supported.setdefault(key, final_url)
            for system, url in systems.items():
                unsupported.setdefault(system, (url, final_url))

        home = self._page(website, res)
        candidates: list[str] = []
        if home:
            own_domains.add(registrable_domain(home[0]))
            own_domains.discard("")
            scan(*home)
            candidates = careers_links(home[1], home[0], own_domains)
        if not candidates:
            base = home[0] if home else website
            candidates = [urljoin(base, path) for path in FALLBACK_CAREERS_PATHS]

        visited = {home[0].rstrip("/")} if home else set()
        deeper: list[str] = []
        fetched = 0
        for url in candidates:
            if fetched >= MAX_CAREERS_PAGES or supported:
                break
            if url.rstrip("/") in visited:
                continue
            visited.add(url.rstrip("/"))
            fetched += 1
            page = self._page(url, res)
            if not page:
                continue
            visited.add(page[0].rstrip("/"))
            if res.careers_url is None:
                res.careers_url = page[0]
            scan(*page)
            deeper += [u for u in careers_links(page[1], page[0], own_domains)
                       if u.rstrip("/") not in visited]
        # One more hop: "Careers" often leads to an "Open roles" page that holds the board.
        if not supported and not unsupported and deeper:
            page = self._page(deeper[0], res)
            if page:
                scan(*page)
        return supported, unsupported

    # -- boards

    def _fetch_board(self, source: str, ref: str, res: Resolution):
        """Raw postings for a board, [] if empty, None if it does not exist."""
        res.requests += 1
        adapter = self.adapter(source)
        try:
            return adapter.items(adapter.fetch_raw(ref), ref)
        except BoardNotFound:
            return None
        except SourceError as exc:
            res.notes.append(f"{source}/{ref}: {exc}")
            return None

    def _check_guess(self, company: str, source: str, ref: str, items: list[dict],
                     own_domains: set[str], res: Resolution) -> Candidate:
        """Decide whether a board found by guessing belongs to this company."""
        cand = Candidate(source, ref, "guess", "unconfirmed", "", roles=len(items))
        texts = [_normalize(json.dumps(item, ensure_ascii=False)) for item in items]
        for domain in sorted(own_domains):
            pattern = domain_pattern(domain)
            hits = sum(1 for text in texts if pattern.search(text))
            if hits and hits / len(items) >= MIN_DOMAIN_SHARE:
                cand.status = "confirmed"
                cand.detail = f"{hits} of {len(items)} postings reference {domain}"
                return cand

        res.requests += 1
        try:
            identity = self.adapter(source).identity(ref)
        except SourceError as exc:
            cand.detail = f"board exists but its identity could not be read ({exc})"
            return cand
        declared = registrable_domain(identity.website)
        if declared and declared in own_domains:
            cand.status = "confirmed"
            cand.detail = f"board declares website {identity.website}"
        elif identity.name and not names_match(identity.name, company):
            cand.status = "rejected"
            cand.detail = f"board belongs to {identity.name!r}, not {company!r}"
        else:
            named = f"board is named {identity.name!r}" if identity.name else "board has no name"
            site = f", declares website {identity.website}" if identity.website else ""
            cand.detail = (f"{named}{site}, but nothing ties it to "
                           f"{' or '.join(sorted(own_domains)) or 'a company domain'}")
        return cand

    # -- one company

    def resolve(self, company: str, website: str) -> Resolution:
        res = Resolution(company=company)
        own_domains = {registrable_domain(website)} - {""}
        supported, unsupported = {}, {}
        if website:
            supported, unsupported = self._site_evidence(website, res, own_domains)
        else:
            res.notes.append("no website on record; only slug guesses are possible")

        # 1. Links from the company's own site.
        for (source, ref), found_on in supported.items():
            items = self._fetch_board(source, ref, res)
            if items is None:
                res.notes.append(f"{found_on} links to {source}/{ref}, but that board does not exist")
                continue
            res.candidates.append(Candidate(
                source, ref, "link", "confirmed", f"linked from {found_on}", roles=len(items)))
        linked = [c for c in res.candidates if c.kind == "link"]
        if linked:
            if len(linked) > MAX_LINKED_BOARDS:
                for cand in linked:
                    cand.status = "unconfirmed"
                res.notes.append(
                    f"the site links to {len(linked)} different boards; confirm which are its own")
                res.outcome = "needs_review"
            else:
                res.outcome = "mapped"
            return res

        # 2. The site points at a job system we have no adapter for: that is
        #    evidence too, and it outranks a slug guess.
        if unsupported:
            system, (url, _) = next(iter(unsupported.items()))
            res.outcome, res.job_system, res.job_system_url = "unsupported", system, url
            for other, (other_url, _) in list(unsupported.items())[1:]:
                res.notes.append(f"also found {other}: {other_url}")
            return res

        # 3. Slug guesses, accepted only with confirmation.
        tried = set(supported)
        for ref in slug_guesses(company, website):
            for source in GUESS_SOURCES:
                if (source, ref) in tried:
                    continue
                tried.add((source, ref))
                items = self._fetch_board(source, ref, res)
                if not items:
                    if items == []:
                        res.notes.append(f"{source}/{ref} exists but is empty; cannot confirm it")
                    continue
                cand = self._check_guess(company, source, ref, items, own_domains, res)
                res.candidates.append(cand)
                if cand.status == "confirmed":
                    res.outcome = "mapped"
                    return res

        if res.careers_url:
            res.job_system, res.job_system_url = CUSTOM, res.careers_url
        if any(c.status == "unconfirmed" for c in res.candidates):
            res.outcome = "needs_review"
        elif res.job_system:
            res.outcome = "unsupported"
        return res


# --- storage -----------------------------------------------------------------

def save_resolution(conn: sqlite3.Connection, res: Resolution) -> None:
    """Save confirmed boards and record what was found."""
    for cand in res.to_save:
        try:
            add_board(conn, res.company, cand.source, cand.ref, origin=cand.kind, evidence=cand.detail)
            cand.saved = True
        except CompanyError as exc:
            cand.status = "unconfirmed"
            res.notes.append(str(exc))
    if res.outcome == "mapped" and not any(c.saved for c in res.candidates):
        res.outcome = "needs_review"
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name = ?", (res.company,)).fetchone()["id"]
    evidence = {"candidates": [asdict(c) for c in res.candidates], "notes": res.notes}
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO company_resolutions (company_id, resolved_at, outcome, "
            "job_system, job_system_url, careers_url, evidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (company_id, utc_now(), res.outcome, res.job_system, res.job_system_url,
             res.careers_url, json.dumps(evidence, ensure_ascii=False)))


def resolve_companies(
    conn: sqlite3.Connection,
    *,
    names: list[str] | None = None,
    dry_run: bool = False,
    delay: float = 1.0,
    resolver: Resolver | None = None,
    on_company: Callable[[Resolution], None] | None = None,
) -> list[Resolution]:
    """Resolve the named companies, or every active company with no board.

    With ``dry_run`` the same lookups run but nothing is written.
    """
    if names:
        targets = []
        for name in names:
            row = conn.execute(
                "SELECT name, website FROM companies WHERE name = ?", (name.strip(),)).fetchone()
            if row is None:
                raise CompanyError(f"no company named {name!r}")
            targets.append(row)
    else:
        targets = conn.execute(
            "SELECT c.name, c.website FROM companies c WHERE c.active = 1 AND NOT EXISTS "
            "(SELECT 1 FROM boards b WHERE b.company_id = c.id) "
            "ORDER BY c.tier IS NULL, c.tier, c.name").fetchall()
    resolver = resolver or Resolver(delay=delay)
    results = []
    for target in targets:
        try:
            res = resolver.resolve(target["name"], target["website"])
        except Exception as exc:  # one company's failure must not stop the rest
            res = Resolution(company=target["name"], notes=[f"resolve failed: {exc!r}"])
        if not dry_run:
            save_resolution(conn, res)
        results.append(res)
        if on_company:
            on_company(res)
    return results


def mapping_report(conn: sqlite3.Connection) -> dict:
    """Where every active company stands: mapped, unsupported system, or unresolved."""
    companies = conn.execute(
        "SELECT c.id, c.name, c.website, r.outcome, r.job_system, r.job_system_url, "
        "r.careers_url, r.evidence, r.resolved_at FROM companies c "
        "LEFT JOIN company_resolutions r ON r.company_id = c.id WHERE c.active = 1 "
        "ORDER BY c.name").fetchall()
    boards: dict[int, list[sqlite3.Row]] = {}
    for b in conn.execute("SELECT * FROM boards ORDER BY id"):
        boards.setdefault(b["company_id"], []).append(b)

    report = {"total": len(companies), "mapped": {}, "unsupported": {}, "unresolved": []}
    for c in companies:
        if c["id"] in boards:
            for b in boards[c["id"]]:
                report["mapped"].setdefault(b["source"], []).append({
                    "company": c["name"], "ref": b["ref"], "origin": b["origin"],
                    "evidence": b["evidence"]})
            continue
        evidence = json.loads(c["evidence"]) if c["evidence"] else {"candidates": [], "notes": []}
        pending = [x for x in evidence["candidates"] if x["status"] == "unconfirmed"]
        if c["job_system"] and not pending:
            report["unsupported"].setdefault(c["job_system"], []).append({
                "company": c["name"], "url": c["job_system_url"]})
            continue
        if c["outcome"] is None:
            reason = "not resolved yet"
        elif pending:
            reason = "needs review"
        else:
            reason = "; ".join(evidence["notes"][:2]) or "no careers page or board found"
        report["unresolved"].append({
            "company": c["name"], "website": c["website"], "reason": reason,
            "candidates": evidence["candidates"]})
    return report
