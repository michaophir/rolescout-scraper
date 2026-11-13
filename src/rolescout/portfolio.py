"""Portfolio job boards as a source of new companies.

A portfolio board (an investor's list of jobs at the companies it backs) is
read for its companies only. Roles on such a board are copies of what each
company's own board has, so none is ever stored: the most that is kept is a
count of how many of a company's open roles match a location rule.

Only boards on the Getro platform can be read. Its public board pages are
used: the sitemap, each company page, and the server-rendered job total for
a filter. Getro's API host disallows crawlers in its robots.txt and is never
called; ``Fetcher`` refuses any URL that is not on the board's own host.
"""

import base64
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from rolescout.db import get_meta, set_meta, transaction, utc_now
from rolescout.http import USER_AGENT, make_session
from rolescout.resolve import read_body, registrable_domain
from rolescout.urls import InvalidWebsite, normalize_website

PLATFORM = "getro"
PAGE_TIMEOUT = 20
MAX_SUB_SITEMAPS = 50
RECHECK_DAYS = 30                  # a company page is re-read after this long
HARVEST_META_KEY = "last_harvest_at"
HARVEST_SLACK = timedelta(hours=12)
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)


class PortfolioError(Exception):
    pass


class Forbidden(PortfolioError):
    """A URL this module must never request."""


# --- the registry ------------------------------------------------------------

def board_base(url: str) -> str:
    """https://host for a board URL given in any form (path and query dropped)."""
    try:
        parts = urlsplit(normalize_website(url))
    except InvalidWebsite as exc:
        raise PortfolioError(str(exc)) from None
    if not parts.netloc:
        raise PortfolioError(f"{url!r} is not a board URL")
    return f"{parts.scheme}://{parts.netloc}"


def add_board(conn: sqlite3.Connection, url: str, name: str = "") -> sqlite3.Row:
    base = board_base(url)
    if conn.execute("SELECT 1 FROM portfolio_boards WHERE url = ?", (base,)).fetchone():
        raise PortfolioError(f"{base} is already registered")
    with transaction(conn):
        conn.execute("INSERT INTO portfolio_boards (url, name, added_at) VALUES (?, ?, ?)",
                     (base, name.strip(), utc_now()))
    return get_board(conn, base)


def get_board(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """A board by id, URL or name."""
    ref = str(ref).strip()
    row = None
    if ref.isdigit():
        row = conn.execute("SELECT * FROM portfolio_boards WHERE id = ?", (int(ref),)).fetchone()
    if row is None:
        try:
            row = conn.execute("SELECT * FROM portfolio_boards WHERE url = ?", (board_base(ref),)).fetchone()
        except PortfolioError:
            row = None
    if row is None:
        row = conn.execute("SELECT * FROM portfolio_boards WHERE name = ? COLLATE NOCASE", (ref,)).fetchone()
    if row is None:
        raise PortfolioError(f"no registered board matches {ref!r}")
    return row


def list_boards(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT b.*, (SELECT COUNT(*) FROM candidate_listings l WHERE l.board_id = b.id) AS candidates "
        "FROM portfolio_boards b ORDER BY b.id")
    return [dict(r) for r in rows]


def remove_board(conn: sqlite3.Connection, ref: str) -> tuple[str, int]:
    """Unregister a board. Candidates it listed stay (they may be on other
    boards, activated or dismissed); only the listing is forgotten.
    Returns (url, candidates left with no board)."""
    board = get_board(conn, ref)
    with transaction(conn):
        conn.execute("DELETE FROM portfolio_boards WHERE id = ?", (board["id"],))
    orphans = conn.execute(
        "SELECT COUNT(*) FROM candidates c WHERE NOT EXISTS "
        "(SELECT 1 FROM candidate_listings l WHERE l.candidate_id = c.id)").fetchone()[0]
    return board["url"], orphans


# --- polite fetching ---------------------------------------------------------

class Fetcher:
    """GETs pages of one board host: one request per ``delay`` seconds,
    robots.txt checked for every URL, every request counted."""

    def __init__(self, session: requests.Session | None = None, delay: float = 1.0) -> None:
        self.session = session or make_session(min_interval=delay)
        self.requests = 0
        self._robots: dict[str, RobotFileParser | None] = {}

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            rules: RobotFileParser | None = RobotFileParser()
            self.requests += 1
            try:
                resp = self.session.get(origin + "/robots.txt", timeout=PAGE_TIMEOUT, stream=True,
                                        headers={"Accept": "text/plain,*/*"})
                if resp.status_code == 200:
                    rules.parse(read_body(resp, 500_000).splitlines())
                elif resp.status_code >= 500:
                    rules.parse(["User-agent: *", "Disallow: /"])
                else:
                    rules = None
            except requests.RequestException:
                rules = None
            self._robots[origin] = rules
        rules = self._robots[origin]
        return rules is None or rules.can_fetch(USER_AGENT, url)

    def get(self, base: str, path: str, accept: str = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"):
        """(status, text). Status is "robots" when robots.txt disallows the URL,
        or an exception name when the request failed."""
        url = path if path.startswith("http") else base + path
        if urlsplit(url).netloc.lower() != urlsplit(base).netloc.lower():
            # In particular the platform's API host, which forbids crawlers.
            raise Forbidden(f"refusing to fetch {url}: only pages on {base} are read")
        if not self._allowed(url):
            return "robots", ""
        self.requests += 1
        try:
            resp = self.session.get(url, timeout=PAGE_TIMEOUT, stream=True, headers={"Accept": accept})
            body = read_body(resp) if resp.status_code == 200 else ""
        except requests.RequestException as exc:
            return type(exc).__name__, ""
        return resp.status_code, body


# --- reading a Getro board's pages -------------------------------------------

def next_data(page: str) -> dict | None:
    match = NEXT_DATA.search(page or "")
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def parse_board_page(page: str) -> dict | None:
    """{"id", "name"} of the board network from its /jobs page, or None if this is not a Getro board."""
    data = next_data(page)
    try:
        network = data["props"]["pageProps"]["network"]
        return {"id": str(network["id"]), "name": network.get("name") or ""}
    except (TypeError, KeyError):
        return None


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """(company slugs, further sitemap URLs) from a board's sitemap."""
    slugs, more = {}, []
    for loc in LOC.findall(xml or ""):
        loc = loc.strip()
        path = urlsplit(loc).path
        company = re.fullmatch(r"/companies/([^/]+)/?", path)
        if company:
            slugs.setdefault(company.group(1))
        elif path.endswith(".xml"):
            more.append(loc)
    return list(slugs), more


def _labels(values) -> list[str]:
    """Tag lists come as plain strings on some pages and as objects with a
    ``description`` or ``name`` on others."""
    out = []
    for value in values or []:
        if isinstance(value, dict):
            value = value.get("description") or value.get("name")
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    return out


def parse_company_page(page: str) -> dict | None:
    """The fields kept for a candidate, from a board's company page."""
    data = next_data(page)
    try:
        company = data["props"]["pageProps"]["company"]
        company["name"]
    except (TypeError, KeyError):
        return None
    website = company.get("websiteUrl") or company.get("domain") or ""
    try:
        website = normalize_website(website)
    except InvalidWebsite:
        website = ""
    sectors = list(dict.fromkeys([*_labels(company.get("industryTags")), *_labels(company.get("topics"))]))
    employees = company.get("approxEmployees")
    return {
        "platform_id": str(company.get("id") or ""),
        "name": (company.get("name") or "").strip(),
        "domain": registrable_domain(company.get("domain") or "") or registrable_domain(website),
        "website": website,
        "stage": (company.get("stage") or "").strip().lower() or None,
        "employees": employees if isinstance(employees, int) and not isinstance(employees, bool) else None,
        "sectors": sectors,
        "locations": _labels(company.get("locations")),
    }


def jobs_filter_path(platform_id: str, locations: list[str]) -> str:
    """The board page whose server-rendered total counts a company's open
    roles in any of ``locations``."""
    spec = {"organization.id": [int(platform_id) if str(platform_id).isdigit() else platform_id],
            "searchable_locations": list(locations)}
    encoded = base64.b64encode(json.dumps(spec, separators=(",", ":")).encode()).decode()
    return "/jobs?filter=" + quote(encoded)


def parse_jobs_total(page: str) -> int | None:
    """The total the page reports. Nothing else on the page is read or kept."""
    data = next_data(page)
    try:
        total = data["props"]["pageProps"]["initialState"]["jobs"]["total"]
    except (TypeError, KeyError):
        return None
    return total if isinstance(total, int) else None


# --- the harvest -------------------------------------------------------------

@dataclass
class BoardHarvest:
    url: str
    name: str = ""
    listed: int = 0            # companies in the sitemap
    read: int = 0              # company pages read this time
    fresh: int = 0             # already known and checked recently: not re-read
    unreadable: int = 0
    no_domain: int = 0
    role_checks: int = 0
    error: str | None = None
    finished: bool = False


@dataclass
class HarvestSummary:
    boards: list[BoardHarvest] = field(default_factory=list)
    requests: int = 0
    seconds: float = 0.0
    stopped_early: bool = False

    @property
    def complete(self) -> bool:
        return not self.stopped_early and all(b.finished or b.error for b in self.boards)


def _upsert_candidate(conn, board_id: int, slug: str, info: dict, now: str) -> int:
    row = conn.execute("SELECT id FROM candidates WHERE domain = ?", (info["domain"],)).fetchone()
    values = (info["name"], info["website"], info["stage"], info["employees"],
              json.dumps(info["sectors"], ensure_ascii=False),
              json.dumps(info["locations"], ensure_ascii=False), now)
    if row is None:
        candidate_id = conn.execute(
            "INSERT INTO candidates (name, website, stage, employees, sectors, locations, "
            "last_checked_at, domain, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*values, info["domain"], now)).lastrowid
    else:
        candidate_id = row["id"]
        conn.execute(
            "UPDATE candidates SET name = ?, website = ?, stage = ?, employees = ?, sectors = ?, "
            "locations = ?, last_checked_at = ? WHERE id = ?", (*values, candidate_id))
    conn.execute(
        "INSERT INTO candidate_listings (candidate_id, board_id, slug, platform_id, listed_at, last_listed_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (candidate_id, board_id) DO UPDATE SET "
        "slug = excluded.slug, platform_id = excluded.platform_id, last_listed_at = excluded.last_listed_at",
        (candidate_id, board_id, slug, info["platform_id"], now, now))
    conn.execute("DELETE FROM portfolio_skips WHERE board_id = ? AND slug = ?", (board_id, slug))
    return candidate_id


def _harvest_board(conn, board, fetcher: Fetcher, out: BoardHarvest, deadline: float,
                   recheck_days: float) -> bool:
    """Read one board's companies. Returns False if the time limit cut it short."""
    base = board["url"]
    status, page = fetcher.get(base, "/jobs")
    network = parse_board_page(page) if status == 200 else None
    if network is None:
        out.error = ("robots.txt disallows reading this board" if status == "robots"
                     else f"could not read {base}/jobs ({status})" if status != 200
                     else "not a Getro board; only Getro boards can be harvested")
        return True
    out.name = board["name"] or network["name"]

    slugs, pending, seen_maps = [], ["/sitemap.xml"], set()
    while pending and len(seen_maps) < MAX_SUB_SITEMAPS:
        path = pending.pop(0)
        if path in seen_maps:
            continue
        seen_maps.add(path)
        status, xml = fetcher.get(base, path, accept="application/xml,text/xml,*/*")
        if status != 200:
            if path == "/sitemap.xml":
                out.error = f"could not read the board's sitemap ({status})"
                return True
            continue
        found, more = parse_sitemap(xml)
        slugs += found
        pending += [m for m in more if urlsplit(m).netloc == urlsplit(base).netloc]
    slugs = list(dict.fromkeys(slugs))
    out.listed = len(slugs)

    now = utc_now()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=recheck_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    known = {r["slug"]: r for r in conn.execute(
        "SELECT l.slug, l.candidate_id, c.last_checked_at FROM candidate_listings l "
        "JOIN candidates c ON c.id = l.candidate_id WHERE l.board_id = ?", (board["id"],))}
    skipped = {r["slug"]: r["checked_at"] for r in conn.execute(
        "SELECT slug, checked_at FROM portfolio_skips WHERE board_id = ?", (board["id"],))}
    with transaction(conn):
        conn.execute("UPDATE portfolio_boards SET name = ?, platform = ?, companies_listed = ?, "
                     "last_error = NULL WHERE id = ?", (out.name, PLATFORM, out.listed, board["id"]))
        # Still listed: note it without a request. No longer listed: forget the listing.
        conn.executemany("UPDATE candidate_listings SET last_listed_at = ? WHERE board_id = ? AND slug = ?",
                         [(now, board["id"], s) for s in slugs if s in known])
        conn.executemany("DELETE FROM candidate_listings WHERE board_id = ? AND slug = ?",
                         [(board["id"], s) for s in known if s not in set(slugs)])

    def last_checked(slug: str) -> str:
        return (known[slug]["last_checked_at"] if slug in known else skipped.get(slug)) or ""
    # Never-read companies first, then the longest unchecked, so a harvest cut
    # short picks up where it stopped.
    todo = sorted((s for s in slugs if last_checked(s) < cutoff), key=last_checked)
    out.fresh = len(slugs) - len(todo)
    for slug in todo:
        if time.monotonic() >= deadline:
            return False
        status, page = fetcher.get(base, f"/companies/{slug}")
        try:
            info = parse_company_page(page) if status == 200 else None
        except Exception:  # one odd page must not end the board's harvest
            info, status = None, "unexpected page shape"
        with transaction(conn):
            if info is None or not info["domain"]:
                reason = "no website domain on its page" if info else f"page not readable ({status})"
                out.no_domain += bool(info)
                out.unreadable += not info
                # Remembered so it is not re-read every time, unless the failure
                # looks temporary (a timeout, a server error): those are tried
                # again at the next harvest.
                if info or status in ("robots", 404, 410, "unexpected page shape"):
                    conn.execute("INSERT OR REPLACE INTO portfolio_skips (board_id, slug, reason, checked_at) "
                                 "VALUES (?, ?, ?, ?)", (board["id"], slug, reason, utc_now()))
            else:
                _upsert_candidate(conn, board["id"], slug, info, utc_now())
                out.read += 1
    return True


def check_roles(conn, fetcher: Fetcher, candidate_ids: list[int], locations: list[str],
                deadline: float, counts: dict[str, BoardHarvest] | None = None) -> bool:
    """For each candidate, ask one of its boards how many of its open roles
    are in ``locations`` and store that number. One request each."""
    rule = json.dumps(sorted(locations))
    for candidate_id in candidate_ids:
        if time.monotonic() >= deadline:
            return False
        listing = conn.execute(
            "SELECT l.platform_id, b.url FROM candidate_listings l JOIN portfolio_boards b "
            "ON b.id = l.board_id WHERE l.candidate_id = ? AND l.platform_id != '' "
            "ORDER BY b.id LIMIT 1", (candidate_id,)).fetchone()
        if listing is None:
            continue
        status, page = fetcher.get(listing["url"], jobs_filter_path(listing["platform_id"], locations))
        total = parse_jobs_total(page) if status == 200 else None
        if counts is not None and listing["url"] in counts:
            counts[listing["url"]].role_checks += 1
        if total is None:
            continue  # stays pending; the next harvest asks again
        with transaction(conn):
            conn.execute("UPDATE candidates SET matching_roles = ?, roles_rule = ?, roles_checked_at = ? "
                         "WHERE id = ?", (total, rule, utc_now(), candidate_id))
    return True


def harvest(conn: sqlite3.Connection, cfg, *, boards: list[str] | None = None, delay: float = 1.0,
            time_limit: float | None = None, recheck_days: float = RECHECK_DAYS,
            fetcher: Fetcher | None = None, on_board=None) -> HarvestSummary:
    """Read registered boards into the candidates list and apply the filter.

    Each company page is committed as it is read, so a harvest stopped by its
    ``time_limit`` (seconds) loses nothing and the next one continues. Only
    new companies and ones last checked more than ``recheck_days`` ago are
    read. The open-role rule costs one request per candidate and is asked
    only for candidates that passed every other rule.
    """
    from rolescout import candidates as rules  # candidates builds on this module

    fetcher = fetcher or Fetcher(delay=delay)
    started = time.monotonic()
    deadline = started + time_limit if time_limit else float("inf")
    targets = [get_board(conn, ref) for ref in boards] if boards else \
        conn.execute("SELECT * FROM portfolio_boards ORDER BY id").fetchall()
    summary = HarvestSummary()
    by_url: dict[str, BoardHarvest] = {}
    try:
        for board in targets:
            out = BoardHarvest(url=board["url"], name=board["name"])
            summary.boards.append(out)
            by_url[board["url"]] = out
            if time.monotonic() >= deadline:
                summary.stopped_early = True
                break
            try:
                out.finished = _harvest_board(conn, board, fetcher, out, deadline, recheck_days)
            except Forbidden:
                raise
            except Exception as exc:  # one board's failure must not stop the others
                out.error = f"{type(exc).__name__}: {exc}"
            with transaction(conn):
                conn.execute("UPDATE portfolio_boards SET last_harvest_at = ?, last_error = ? WHERE id = ?",
                             (utc_now(), out.error, board["id"]))
            if on_board:
                on_board(out)
            if not out.finished and not out.error:
                summary.stopped_early = True
                break
        # The filter needs no network; the role rule does, for those still in the running.
        pending = rules.apply_filter(conn, cfg)
        if pending and cfg.require_open_role and not summary.stopped_early:
            if not check_roles(conn, fetcher, pending, cfg.role_locations, deadline, by_url):
                summary.stopped_early = True
            rules.apply_filter(conn, cfg)
        elif pending and cfg.require_open_role:
            summary.stopped_early = True
    finally:
        summary.requests = fetcher.requests
        summary.seconds = time.monotonic() - started
    return summary


def harvest_due(conn: sqlite3.Connection, every_days: float, now: datetime | None = None) -> bool:
    """Whether the periodic harvest should run now. Never run counts as due."""
    if every_days <= 0 or not conn.execute("SELECT 1 FROM portfolio_boards LIMIT 1").fetchone():
        return False
    last = get_meta(conn, HARVEST_META_KEY)
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    then = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return now - then >= timedelta(days=every_days) - HARVEST_SLACK


def mark_harvested(conn: sqlite3.Connection, now: str | None = None) -> None:
    set_meta(conn, HARVEST_META_KEY, now or utc_now())
