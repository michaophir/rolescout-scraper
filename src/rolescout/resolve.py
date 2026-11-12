"""Find each company's job board once, with evidence, and cache the mapping.

Two kinds of evidence, in order:

1. **Link** (strong). The company's own website or careers page links to a
   board on a supported source, and that board exists. Saved automatically.
2. **Guess** (weak). A board exists at a slug derived from the company name
   or domain. Saved only when something ties the board to the company's own
   domain; a matching name alone is never enough, because namesakes exist.

Anything else is reported and left unmapped. When the careers page points at
a job system we have no adapter for, that system and URL are recorded.

Careers pages that render their listings with JavaScript still tend to name
their job system in the static HTML: an embed script, an API hostname, a CSS
hook, a board token in inline config. Those fingerprints identify the system
even when no board link is present.
"""

import html
import json
import re
import signal
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from rolescout.adapters import ADAPTERS, BoardNotFound, SourceError, get_adapter
from rolescout.companies import CompanyError, add_board
from rolescout.db import transaction, utc_now
from rolescout.http import USER_AGENT, make_session

PAGE_TIMEOUT = 20

# --- limits: resolving one company is bounded whatever a page contains -------
# Structural limits make the work small; the time budgets are the backstop.
PAGE_MAX_BYTES = 2_000_000          # downloaded and scanned per page or script
COMPANY_SCAN_MAX_CHARS = 8_000_000  # scanned per company, all pages together
# Longest unbroken run of letters, digits, _ % - kept before scanning. A real
# hostname label is at most 63 characters; what is longer is encoded data
# (one site embeds a 110,000-character base64 blob), and unbroken runs are
# what make a pattern slow.
MAX_TOKEN_CHARS = 80
COMPANY_WALL_SECONDS = 60.0         # per company, start to finish
COMPANY_CPU_SECONDS = 15.0          # per company, processor time
MAX_CAREERS_PAGES = 2          # careers pages followed from the homepage
MAX_OWN_SCRIPTS = 2            # the site's own script files read when the pages name no board
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
    ("greenhouse", re.compile(r"(?<![\w-])(?:boards-)?api\.greenhouse\.io/v1/boards/([A-Za-z0-9_]+)", re.I)),
    ("greenhouse", re.compile(r"(?<![\w.-])(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_]+)", re.I)),
    ("lever", re.compile(r"(?<![\w.-])jobs\.lever\.co/" + _SLUG, re.I)),
    ("lever", re.compile(r"(?<![\w.-])api\.lever\.co/v0/postings/" + _SLUG, re.I)),
    ("ashby", re.compile(r"(?<![\w.-])jobs\.ashbyhq\.com/" + _SLUG, re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _SLUG, re.I)),
]
NOT_A_SLUG = {"embed", "api", "v1", "jobs", "job", "static", "assets", "js", "css"}

# A Greenhouse board fetched with the token held in a variable, e.g.
#   const company = "acme"; fetch("https://boards-api.greenhouse.io/v1/boards/" + company + ...)
GREENHOUSE_DYNAMIC = re.compile(r"greenhouse\.io/v1/boards/[\"'`]\s*\+|greenhouse\.io/v1/boards/\$\{", re.I)
BOARD_TOKEN_VAR = re.compile(
    r"\b(?:company|board|boardToken|board_token|boardName|boardId|ghBoard|greenhouseBoard(?:Token)?)"
    r"\s*[:=]\s*[\"'`]([A-Za-z0-9_]{2,40})[\"'`]", re.I)

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
    ("paycom", r"(?:www\.)?paycomonline\.net/v4/ats/web\.php/(?:portal|jobs)/[A-Za-z0-9]+"),
    ("greenhouse-eu", r"(?:job-boards|boards)\.eu\.greenhouse\.io/[\w-]+"),
    ("lever-eu", r"jobs\.eu\.lever\.co/[\w.-]+"),
]
UNSUPPORTED_LINKS = [
    (system, re.compile(r"(?<![\w.-])(?:" + pattern + r")[^\s\"'<>\\)]*", re.I))
    for system, pattern in UNSUPPORTED_SYSTEMS
]
CUSTOM = "custom"  # a hand-recorded "they built their own"; never assigned automatically

# Marks a job system leaves in a page even when it gives no board link:
# embed scripts, API hosts, CSS hooks. They identify the system, not the board.
# Keep these free of leading wildcards: they run over megabytes of minified script.
FINGERPRINTS: list[tuple[str, re.Pattern]] = [(system, re.compile(pattern, re.I)) for system, pattern in [
    ("greenhouse", r"(?:boards-)?api\.greenhouse\.io|boards\.greenhouse\.io/embed|\bgrnhse_|"
                   r"greenhouse[_-]job|js-greenhouse|[?&]gh_jid="),
    ("lever", r"api\.lever\.co/v0/postings|jobs\.lever\.co"),
    ("ashby", r"(?:jobs|api)\.ashbyhq\.com|_ashby_embed|ashby_jid"),
    ("workable", r"workable\.com/assets/embed\.js|\bwhr_embed\s*\("),
    ("paycom", r"paycomonline\.net/v4/ats"),
    ("rippling", r"ats\.rippling\.com"),
    ("workday", r"myworkday(?:jobs|site)\.com"),
    ("smartrecruiters", r"(?:static|api|jobs|careers)\.smartrecruiters\.com"),
    ("eightfold", r"\.eightfold\.ai|eightfold-careers"),
    ("icims", r"\.icims\.com"),
    ("jobvite", r"jobs\.jobvite\.com|jobvite\.com/(?:CompanyJobs|careers)"),
    ("bamboohr", r"\.bamboohr\.com/(?:careers|jobs|js/embed)"),
    ("teamtailor", r"\.teamtailor\.com|teamtailor-cdn"),
    ("phenom", r"cdn\.phenompeople\.com|phenompeople\.com/CareerConnectResources"),
    ("gem", r"jobs\.gem\.com"),
    ("personio", r"\.jobs\.personio\."),
    ("recruitee", r"(?<!www)\.recruitee\.com"),
]]

OPEN_ROLES_HINT = re.compile(r"open|roles|positions|openings|search|listing|vacanc|/jobs", re.I)
LOCALE_SEGMENT = re.compile(r"/[a-z]{2}(?:-[a-z]{2})?(?:/|$)", re.I)

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
    over_budget: bool = False     # stopped by the per-company time budget

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


_LONG_TOKEN = re.compile(r"([\w%-]{" + str(MAX_TOKEN_CHARS) + r"})[\w%-]+")


def _normalize(text: str) -> str:
    """Undo the escaping that hides URLs inside HTML attributes and inline JSON,
    and cut unbroken runs of word characters down to ``MAX_TOKEN_CHARS``."""
    text = html.unescape(text)
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    return _LONG_TOKEN.sub(r"\1", text)


class BudgetExceeded(Exception):
    """Resolving one company went over its time budget."""


@contextmanager
def time_budget(wall: float, cpu: float):
    """Interrupt the enclosed block with ``BudgetExceeded`` once it has run for
    ``wall`` seconds or used ``cpu`` seconds of processor time.

    Uses interval timers, so it can stop a slow download or a slow pattern
    match that would never reach a cooperative check. Timers need the main
    thread on a Unix system; elsewhere this does nothing and the resolver's
    own checks between steps (``Resolver._tick``) are the only budget.
    """
    usable = (hasattr(signal, "setitimer")
              and threading.current_thread() is threading.main_thread())
    if not usable:
        yield
        return

    def over_wall(signum, frame):
        raise BudgetExceeded(f"took longer than {wall:g} seconds")

    def over_cpu(signum, frame):
        raise BudgetExceeded(f"used more than {cpu:g} seconds of processor time")

    old_wall = signal.signal(signal.SIGALRM, over_wall)
    old_cpu = signal.signal(signal.SIGVTALRM, over_cpu)
    signal.setitimer(signal.ITIMER_REAL, wall)
    signal.setitimer(signal.ITIMER_VIRTUAL, cpu)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.setitimer(signal.ITIMER_VIRTUAL, 0)
        signal.signal(signal.SIGALRM, old_wall)
        signal.signal(signal.SIGVTALRM, old_cpu)


def read_body(resp, limit: int = PAGE_MAX_BYTES) -> str:
    """At most ``limit`` bytes of a response, as text. Stops downloading there."""
    if not hasattr(resp, "iter_content"):
        return (resp.text or "")[:limit]
    chunks, size = [], 0
    try:
        for chunk in resp.iter_content(65536):
            chunks.append(chunk)
            size += len(chunk)
            if size >= limit:
                break
    finally:
        resp.close()
    data = b"".join(chunks)[:limit]
    # Trust a charset the server states; never guess one (guessing reads the
    # whole body and is slow on large files).
    declared = "charset=" in (resp.headers.get("content-type") or "").lower()
    try:
        return data.decode((resp.encoding if declared else None) or "utf-8", errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def scan_for_boards(text: str, *, normalized: bool = False
                    ) -> tuple[dict[tuple[str, str], None], dict[str, str]]:
    """(supported boards as {(source, slug)}, unsupported systems as {system: url})."""
    if not normalized:
        text = _normalize(text)
    supported: dict[tuple[str, str], None] = {}
    for source, pattern in SUPPORTED_LINKS:
        for match in pattern.finditer(text):
            slug = unquote(match.group(1)).strip(".")
            if source != "ashby":
                slug = slug.lower()
            if slug and slug.lower() not in NOT_A_SLUG:
                supported.setdefault((source, slug))
    if GREENHOUSE_DYNAMIC.search(text):
        for match in BOARD_TOKEN_VAR.finditer(text):
            supported.setdefault(("greenhouse", match.group(1).lower()))
    unsupported: dict[str, str] = {}
    for system, pattern in UNSUPPORTED_LINKS:
        match = pattern.search(text)
        if match:
            unsupported[system] = "https://" + match.group(0).rstrip(".,;")
    return supported, unsupported


def scan_for_fingerprints(text: str, *, normalized: bool = False) -> dict[str, str]:
    """Job systems a page shows signs of, as {system: the text that gave it away}."""
    if not normalized:
        text = _normalize(text)
    found: dict[str, str] = {}
    for system, pattern in FINGERPRINTS:
        match = pattern.search(text)
        if match:
            found[system] = match.group(0).strip("?&=( ")
    return found


def own_scripts(page_html: str, page_url: str, own_domains: set[str]) -> list[str]:
    """Script files the page loads from the company's own domain, in page order."""
    urls: dict[str, None] = {}
    for src in re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", page_html, re.I):
        url = urljoin(page_url, html.unescape(src))
        if registrable_domain(url) in own_domains and urlparse(url).path.endswith(".js"):
            urls.setdefault(url)
    return list(urls)


def rank_deeper_links(links: list[str], company: str) -> list[str]:
    """Order second-hop links: ones naming the company or "open roles" first,
    translated copies of the careers page last."""
    name = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")

    def score(url: str) -> int:
        path = urlparse(url).path.lower()
        points = 0
        if name and len(name) > 3 and (name in path or name.replace("-", "") in path.replace("-", "")):
            points += 2
        if OPEN_ROLES_HINT.search(path):
            points += 1
        if LOCALE_SEGMENT.search(path):
            points -= 2
        return points
    return sorted(links, key=lambda u: -score(u))  # stable: keeps the earlier order within a score


def careers_links(page_html: str, page_url: str, own_domains: set[str], company: str = "") -> list[str]:
    """Links on a page that look like they lead to the company's careers page.

    A link to another site needs more than "career" in its path, or a news
    article about careers would qualify: its link text must say careers or
    jobs, its host must (jobs.example.com, examplecareers.com), or its path
    must name the company (parent.com/careers/company-name).
    """
    found: dict[str, int] = {}
    name = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
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
        if not own and not (
                by_text or re.search(r"career|jobs", parsed.hostname, re.I)
                or (len(name) > 3 and name in parsed.path.lower())):
            continue
        # Prefer links on the company's own domain, then ones matching on both.
        score = (2 if own else 0) + by_href + by_text
        if url.rstrip("/") != page_url.rstrip("/"):
            found[url] = max(found.get(url, 0), score)
    return sorted(found, key=lambda u: (-found[u], len(u)))


# --- the resolver ------------------------------------------------------------

class Resolver:
    def __init__(self, session: requests.Session | None = None, delay: float = 1.0, *,
                 wall_budget: float = COMPANY_WALL_SECONDS,
                 cpu_budget: float = COMPANY_CPU_SECONDS) -> None:
        self.session = session or make_session(min_interval=delay)
        self.wall_budget = wall_budget
        self.cpu_budget = cpu_budget
        self._adapters: dict[str, object] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._deadline = self._cpu_deadline = float("inf")
        self._scanned = 0

    def _tick(self) -> None:
        """Budget check between steps. Works everywhere, including where the
        interval timers of ``time_budget`` cannot be used."""
        if time.monotonic() > self._deadline:
            raise BudgetExceeded(f"took longer than {self.wall_budget:g} seconds")
        if time.process_time() > self._cpu_deadline:
            raise BudgetExceeded(f"used more than {self.cpu_budget:g} seconds of processor time")

    def adapter(self, source: str):
        if source not in self._adapters:
            self._adapters[source] = get_adapter(source, self.session)
        return self._adapters[source]

    # -- pages

    def _allowed(self, url: str, res: Resolution) -> bool:
        """Whether the site's robots.txt lets us read this page.

        One robots.txt request per site, cached. A missing file (any 4xx)
        allows everything; a server error disallows everything, as RFC 9309
        asks. Applies to company websites only, never to the job board APIs.
        """
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            self._tick()
            rules: RobotFileParser | None = RobotFileParser()
            res.requests += 1
            try:
                resp = self.session.get(
                    origin + "/robots.txt", timeout=PAGE_TIMEOUT, stream=True,
                    headers={"Accept": "text/plain,*/*"})
                if resp.status_code == 200:
                    rules.parse(read_body(resp, 500_000).splitlines())
                elif resp.status_code >= 500:
                    rules.parse(["User-agent: *", "Disallow: /"])
                else:
                    rules = None
            except requests.RequestException:
                rules = None  # unreachable: the page request will report that itself
            self._robots[origin] = rules
        rules = self._robots[origin]
        return rules is None or rules.can_fetch(USER_AGENT, url)

    def _page(self, url: str, res: Resolution, *,
              accept: str = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5") -> tuple[str, str] | None:
        """(final URL, body) or None. Never raises."""
        if not self._allowed(url, res):
            res.notes.append(f"robots.txt disallows {url}; skipped")
            return None
        self._tick()
        res.requests += 1
        try:
            resp = self.session.get(
                url, timeout=PAGE_TIMEOUT, stream=True, headers={"Accept": accept})
            body = read_body(resp) if resp.status_code == 200 else ""
        except requests.RequestException as exc:
            res.notes.append(f"could not fetch {url}: {type(exc).__name__}")
            return None
        if resp.status_code != 200:
            res.notes.append(f"could not fetch {url}: HTTP {resp.status_code}")
            return None
        return (resp.url or url), body

    def _site_evidence(self, company: str, website: str, res: Resolution, own_domains: set[str]):
        """Read the homepage, up to two careers pages, one page further in, and
        if still nothing names a board, up to two of the site's own scripts.

        Returns (supported {(source, slug): found_on},
                 unsupported {system: (url, found_on)},
                 fingerprints {system: (matched text, found_on)}).
        """
        supported: dict[tuple[str, str], str] = {}
        unsupported: dict[str, tuple[str, str]] = {}
        fingerprints: dict[str, tuple[str, str]] = {}

        def scan(final_url: str, body: str) -> None:
            self._tick()
            if self._scanned + len(body) > COMPANY_SCAN_MAX_CHARS:
                res.notes.append(
                    f"{final_url} not scanned: this company's pages already exceed "
                    f"{COMPANY_SCAN_MAX_CHARS // 1_000_000} MB")
                body = ""
            self._scanned += len(body)
            # The final URL counts too: /careers often redirects to the job system.
            text = _normalize(final_url + "\n" + body)  # once per page, shared by both scans
            boards, systems = scan_for_boards(text, normalized=True)
            for key in boards:
                supported.setdefault(key, final_url)
            for system, url in systems.items():
                unsupported.setdefault(system, (url, final_url))
            for system, matched in scan_for_fingerprints(text, normalized=True).items():
                fingerprints.setdefault(system, (matched, final_url))

        home = self._page(website, res)
        candidates: list[str] = []
        if home:
            own_domains.add(registrable_domain(home[0]))
            own_domains.discard("")
            scan(*home)
            candidates = careers_links(home[1], home[0], own_domains, company)
        if not candidates:
            base = home[0] if home else website
            candidates = [urljoin(base, path) for path in FALLBACK_CAREERS_PATHS]

        visited = {home[0].rstrip("/")} if home else set()
        deeper: list[str] = []
        careers_pages: list[tuple[str, str]] = []
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
            careers_pages.append(page)
            scan(*page)
            deeper += [u for u in careers_links(page[1], page[0], own_domains, company)
                       if u.rstrip("/") not in visited]
        # One more hop: "Careers" often leads to an "Open roles" page that holds the board.
        if not supported and not unsupported and deeper:
            page = self._page(rank_deeper_links(list(dict.fromkeys(deeper)), company)[0], res)
            if page:
                careers_pages.append(page)
                scan(*page)
        # Still no board: the token is often in the site's own script bundle.
        if not supported and not unsupported:
            scripts = [u for body_url, body in reversed(careers_pages)
                       for u in own_scripts(body, body_url, own_domains)]
            for url in list(dict.fromkeys(scripts))[:MAX_OWN_SCRIPTS]:
                script = self._page(url, res, accept="*/*")
                if script:
                    scan(url, script[1])
                if supported:
                    break
        return supported, unsupported, fingerprints

    # -- boards

    def _fetch_board(self, source: str, ref: str, res: Resolution):
        """Raw postings for a board, [] if empty, None if it does not exist."""
        self._tick()
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
        self._tick()
        texts = [_normalize(json.dumps(item, ensure_ascii=False)[:PAGE_MAX_BYTES]) for item in items]
        for domain in sorted(own_domains):
            pattern = domain_pattern(domain)
            hits = sum(1 for text in texts if pattern.search(text))
            if hits and hits / len(items) >= MIN_DOMAIN_SHARE:
                cand.status = "confirmed"
                cand.detail = f"{hits} of {len(items)} postings reference {domain}"
                return cand

        self._tick()
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
        """Find the company's board, within the time budget.

        If the budget runs out the company comes back unidentified, with a
        note saying so; the caller carries on with the next company.
        """
        res = Resolution(company=company)
        self._scanned = 0
        self._deadline = time.monotonic() + self.wall_budget
        self._cpu_deadline = time.process_time() + self.cpu_budget
        try:
            with time_budget(self.wall_budget, self.cpu_budget):
                self._resolve(res, company, website)
        except BudgetExceeded as exc:
            res.outcome, res.job_system, res.job_system_url = "unresolved", None, None
            res.candidates = []
            res.over_budget = True
            res.notes.append(f"stopped: resolving this company {exc}; left unidentified")
        finally:
            self._deadline = self._cpu_deadline = float("inf")
        return res

    def _resolve(self, res: Resolution, company: str, website: str) -> Resolution:
        own_domains = {registrable_domain(website)} - {""}
        supported, unsupported, fingerprints = {}, {}, {}
        if website:
            supported, unsupported, fingerprints = self._site_evidence(
                company, website, res, own_domains)
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
        #    evidence too, and it outranks a slug guess. A URL on that system
        #    beats a bare fingerprint.
        if unsupported:
            system, (url, _) = next(iter(unsupported.items()))
            res.outcome, res.job_system, res.job_system_url = "unsupported", system, url
            for other, (other_url, _) in list(unsupported.items())[1:]:
                res.notes.append(f"also found {other}: {other_url}")
            return res
        foreign = {k: v for k, v in fingerprints.items() if k not in ADAPTERS}
        if foreign:
            system, (matched, found_on) = next(iter(foreign.items()))
            res.outcome, res.job_system, res.job_system_url = "unsupported", system, found_on
            res.notes.append(f"identified as {system} by {matched!r} on {found_on}")
            for other, (other_matched, _) in list(foreign.items())[1:]:
                res.notes.append(f"also shows signs of {other}: {other_matched!r}")
            return res

        # 3. Slug guesses, accepted only with confirmation. If the site shows a
        #    supported system's fingerprint, only that system is tried.
        known = [k for k in fingerprints if k in ADAPTERS]
        sources = tuple(known) or GUESS_SOURCES
        tried = set(supported)
        for ref in slug_guesses(company, website):
            for source in sources:
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

        if known:
            # The system is one we support; only the board name is missing.
            system = known[0]
            matched, found_on = fingerprints[system]
            res.job_system, res.job_system_url = system, found_on
            res.notes.append(
                f"{found_on} uses {system} ({matched!r}), but the board name was not found; "
                f"map it by hand: rolescout companies map \"{company}\" {system} BOARD")
            res.outcome = "needs_review"
        elif any(c.status == "unconfirmed" for c in res.candidates):
            res.outcome = "needs_review"
        elif res.careers_url:
            res.notes.append(f"careers page {res.careers_url} names no job system we recognize")
        return res


# --- storage -----------------------------------------------------------------

def save_resolution(conn: sqlite3.Connection, res: Resolution) -> None:
    """Save confirmed boards and record what was found.

    A job system recorded by hand (``set_job_system``) is never overwritten:
    boards are still saved, but the hand-written finding stays.
    """
    company_id = conn.execute(
        "SELECT id FROM companies WHERE name = ?", (res.company,)).fetchone()["id"]
    already = {(b["source"], b["ref"]) for b in conn.execute(
        "SELECT source, ref FROM boards WHERE company_id = ?", (company_id,))}
    for cand in res.to_save:
        if (cand.source, cand.ref) in already:
            continue  # re-resolved to the board it already has
        try:
            add_board(conn, res.company, cand.source, cand.ref, origin=cand.kind, evidence=cand.detail)
            cand.saved = True
        except CompanyError as exc:
            cand.status = "unconfirmed"
            res.notes.append(str(exc))
    if res.outcome == "mapped" and not res.to_save:
        res.outcome = "needs_review"
    manual = conn.execute(
        "SELECT manual FROM company_resolutions WHERE company_id = ?", (company_id,)).fetchone()
    if manual is not None and manual["manual"]:
        res.notes.append("job system was recorded by hand; automatic findings were not saved over it")
        return
    if res.over_budget:
        # Running out of time taught us nothing new: keep an earlier finding
        # and add the note to it, rather than replace it with "unidentified".
        earlier = conn.execute(
            "SELECT evidence FROM company_resolutions WHERE company_id = ?", (company_id,)).fetchone()
        if earlier is not None:
            kept = json.loads(earlier["evidence"])
            kept["notes"] = [n for n in kept.get("notes", []) if not n.startswith("stopped: ")]
            kept["notes"].append(f"{utc_now()[:10]} {res.notes[-1]}")
            with transaction(conn):
                conn.execute("UPDATE company_resolutions SET evidence = ? WHERE company_id = ?",
                             (json.dumps(kept, ensure_ascii=False), company_id))
            return
    evidence = {"candidates": [asdict(c) for c in res.candidates], "notes": res.notes}
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO company_resolutions (company_id, resolved_at, outcome, "
            "job_system, job_system_url, careers_url, evidence, manual) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (company_id, utc_now(), res.outcome, res.job_system, res.job_system_url,
             res.careers_url, json.dumps(evidence, ensure_ascii=False)))


def set_job_system(conn: sqlite3.Connection, name: str, system: str, url: str, note: str = "") -> None:
    """Record by hand which job system a company uses. Survives every later resolve."""
    row = conn.execute("SELECT id, name FROM companies WHERE name = ?", (name.strip(),)).fetchone()
    if row is None:
        raise CompanyError(f"no company named {name!r}")
    system = re.sub(r"[^a-z0-9]+", "-", system.strip().lower()).strip("-")
    url = url.strip()
    if not system:
        raise CompanyError("job system name is empty")
    if system in ADAPTERS:
        raise CompanyError(
            f"{system} is a supported source: map the board instead with "
            f"rolescout companies map \"{row['name']}\" {system} BOARD")
    if urlparse(url).scheme not in ("http", "https"):
        raise CompanyError(f"URL must start with http:// or https://, got {url!r}")
    evidence = {"candidates": [], "notes": [note.strip()] if note.strip() else []}
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO company_resolutions (company_id, resolved_at, outcome, "
            "job_system, job_system_url, careers_url, evidence, manual) "
            "VALUES (?, ?, 'unsupported', ?, ?, "
            "(SELECT careers_url FROM company_resolutions WHERE company_id = ?), ?, 1)",
            (row["id"], utc_now(), system, url, row["id"], json.dumps(evidence, ensure_ascii=False)))


def clear_job_system(conn: sqlite3.Connection, name: str) -> bool:
    """Forget a hand-recorded job system so automatic detection applies again."""
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (name.strip(),)).fetchone()
    if row is None:
        raise CompanyError(f"no company named {name!r}")
    with transaction(conn):
        return conn.execute(
            "DELETE FROM company_resolutions WHERE company_id = ? AND manual = 1",
            (row["id"],)).rowcount > 0


def recheck_targets(conn: sqlite3.Connection, failing: list[dict]) -> list[sqlite3.Row]:
    """Who the periodic re-check looks at: active companies with no board
    (unless their job system was recorded by hand), plus the owners of
    ``failing`` boards (see ``ops.failing_boards``)."""
    names = {b["company"] for b in failing}
    rows = conn.execute(
        "SELECT c.name, c.website, "
        "EXISTS (SELECT 1 FROM boards b WHERE b.company_id = c.id) AS mapped, "
        "COALESCE((SELECT manual FROM company_resolutions r WHERE r.company_id = c.id), 0) AS manual, "
        "(SELECT resolved_at FROM company_resolutions r WHERE r.company_id = c.id) AS resolved_at "
        # Longest-unchecked first, so a run cut short by its time limit
        # reaches the rest next time instead of repeating the same ones.
        "FROM companies c WHERE c.active = 1 "
        "ORDER BY resolved_at IS NOT NULL, resolved_at, c.tier IS NULL, c.tier, c.name").fetchall()
    return [r for r in rows if r["name"] in names or (not r["mapped"] and not r["manual"])]


def resolve_companies(
    conn: sqlite3.Connection,
    *,
    names: list[str] | None = None,
    failing: list[dict] | None = None,
    dry_run: bool = False,
    delay: float = 1.0,
    resolver: Resolver | None = None,
    on_company: Callable[[Resolution], None] | None = None,
    time_limit: float | None = None,
    not_checked: list[str] | None = None,
) -> list[Resolution]:
    """Resolve the named companies; or, with none named, every active company
    that has no board and no hand-recorded job system, plus the owners of any
    ``failing`` boards.

    With ``dry_run`` the same lookups run but nothing is written.

    ``time_limit`` (seconds) bounds the whole pass: once it has passed, no
    further company is started and the names left over are appended to
    ``not_checked``. With the per-company budget, the pass ends within
    ``time_limit`` plus one company's budget.
    """
    started = time.monotonic()
    if names:
        targets = []
        for name in names:
            row = conn.execute(
                "SELECT name, website FROM companies WHERE name = ?", (name.strip(),)).fetchone()
            if row is None:
                raise CompanyError(f"no company named {name!r}")
            targets.append(row)
    else:
        targets = recheck_targets(conn, failing or [])
    resolver = resolver or Resolver(delay=delay)
    results = []
    for position, target in enumerate(targets):
        if time_limit is not None and time.monotonic() - started >= time_limit:
            if not_checked is not None:
                not_checked.extend(t["name"] for t in targets[position:])
            break
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
    from rolescout.gaps import classify  # gaps builds on this module's tables

    states = {e["company"]: e for e in classify(conn)}
    report = {"total": len(states), "mapped": {}, "unsupported": {}, "unresolved": []}
    for b in conn.execute(
            "SELECT b.*, c.name FROM boards b JOIN companies c ON c.id = b.company_id "
            "WHERE c.active = 1 ORDER BY c.name, b.id"):
        report["mapped"].setdefault(b["source"], []).append({
            "company": b["name"], "ref": b["ref"], "origin": b["origin"], "evidence": b["evidence"]})
    for name in sorted(states):
        e = states[name]
        if e["state"] == "unsupported":
            report["unsupported"].setdefault(e["system"], []).append({"company": name, "url": e["url"]})
        elif e["state"] == "needs_mapping":
            report["unresolved"].append({
                "company": name, "website": e["website"], "reason": "needs review",
                "candidates": e["candidates"], "notes": e["notes"]})
        elif e["state"] == "unidentified" and not e["failing_boards"]:
            report["unresolved"].append({
                "company": name, "website": e["website"], "reason": e["reason"],
                "candidates": [], "notes": e["notes"]})
    return report
