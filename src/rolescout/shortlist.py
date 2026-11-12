"""The daily shortlist: the open roles to look at first, ranked by a config file.

Ranking reads the local database and one TOML file that lives next to it.
No network, no profile, no model: every point a role gets comes from a rule
in that file, and is reported back in plain words.
"""

import csv
import hashlib
import json
import os
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rolescout.db import transaction, utc_now

CONFIG_NAME = "ranking.toml"
CONFIG_ENV_VAR = "ROLESCOUT_RANKING"
KEEP_DAYS = 30
MARKS = ("dismissed", "saved", "applied")
HIDDEN_MARKS = ("dismissed", "applied")     # never shortlisted again
KEEP_DESCRIPTION_MARKS = ("saved", "applied")


class ConfigError(Exception):
    """The ranking config is missing or not usable."""


class MarkError(Exception):
    pass


# --- the config --------------------------------------------------------------

def config_path(db_path: str | os.PathLike, flag: str | os.PathLike | None = None) -> Path:
    """--config flag, then $ROLESCOUT_RANKING, then ranking.toml next to the database."""
    chosen = flag or os.environ.get(CONFIG_ENV_VAR)
    return Path(chosen).expanduser() if chosen else Path(db_path).parent / CONFIG_NAME


def _phrase(phrase: str) -> re.Pattern:
    """Whole words, any case, any spacing: "VP" matches "VP, Product" but not "SVP"."""
    words = [re.escape(w) for w in phrase.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)


@dataclass
class RankingConfig:
    size: int = 100
    tier_points: dict[int, float] = field(default_factory=dict)
    wanted: dict[str, float] = field(default_factory=dict)       # title phrase -> points
    exclude: list[str] = field(default_factory=list)             # title phrases never wanted
    seniority: dict[str, float] = field(default_factory=dict)    # word -> points
    preferred_locations: dict[str, float] = field(default_factory=dict)
    remote_points: float = 0.0
    recency_max_points: float = 0.0
    recency_full_days: int = 0     # first seen this recently: full points
    recency_zero_days: int = 30    # first seen this long ago or more: none
    source: str = ""               # where it was loaded from
    digest: str = ""               # changes whenever the file's content does

    def __post_init__(self) -> None:
        self._wanted = [(p, pts, _phrase(p)) for p, pts in self.wanted.items()]
        self._exclude = [(p, _phrase(p)) for p in self.exclude]
        self._seniority = [(p, pts, _phrase(p)) for p, pts in self.seniority.items()]
        self._locations = [(p, pts, re.compile(re.escape(p), re.I))
                           for p, pts in self.preferred_locations.items()]


def _points_table(raw: object, where: str) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table of name = points")
    table = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: {key!r} must have a number of points, got {value!r}")
        if not str(key).strip():
            raise ConfigError(f"{where}: empty name")
        table[str(key).strip()] = float(value)
    return table


def _number(section: dict, key: str, default: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key} must be a number, got {value!r}")
    return value


def _only(section: object, allowed: set[str], where: str) -> dict:
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"[{where}] must be a table")
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown setting {', '.join(unknown)} in [{where}]; expected {', '.join(sorted(allowed))}")
    return section


def parse_config(text: str, source: str = "") -> RankingConfig:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source or 'ranking config'} is not valid TOML: {exc}") from None
    raw = _only(raw, {"size", "tier_points", "title", "seniority", "location", "recency"},
                "top level")
    title = _only(raw.get("title"), {"wanted", "exclude"}, "title")
    location = _only(raw.get("location"), {"preferred", "remote_points"}, "location")
    recency = _only(raw.get("recency"), {"max_points", "full_days", "zero_days"}, "recency")

    size = raw.get("size", 100)
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ConfigError(f"size must be a whole number of 1 or more, got {size!r}")
    exclude = title.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(x, str) and x.strip() for x in exclude):
        raise ConfigError("title.exclude must be a list of phrases")
    tiers = {}
    for key, value in _points_table(raw.get("tier_points"), "[tier_points]").items():
        if not key.isdigit():
            raise ConfigError(f"[tier_points]: {key!r} is not a tier number")
        tiers[int(key)] = value
    full, zero = (int(_number(recency, "full_days", 0, "recency")),
                  int(_number(recency, "zero_days", 30, "recency")))
    if zero <= full:
        raise ConfigError("recency.zero_days must be larger than recency.full_days")
    return RankingConfig(
        size=size, tier_points=tiers,
        wanted=_points_table(title.get("wanted"), "[title.wanted]"),
        exclude=[x.strip() for x in exclude],
        seniority=_points_table(_only(raw.get("seniority"), {"words"}, "seniority").get("words"),
                                "[seniority.words]"),
        preferred_locations=_points_table(location.get("preferred"), "[location.preferred]"),
        remote_points=float(_number(location, "remote_points", 0, "location")),
        recency_max_points=float(_number(recency, "max_points", 0, "recency")),
        recency_full_days=full, recency_zero_days=zero,
        source=source, digest=hashlib.sha256(text.encode()).hexdigest()[:12])


def load_config(path: str | os.PathLike) -> RankingConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"no ranking config at {path}. Create one with: rolescout shortlist init")
    return parse_config(path.read_text(encoding="utf-8"), str(path))


def read_filters_csv(path: str | os.PathLike) -> tuple[list[str], list[str]]:
    """(title phrases, seniority words) from a role_filters.csv-style file (field,value)."""
    titles, seniority = [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            kind, value = (row.get("field") or "").strip().lower(), (row.get("value") or "").strip()
            if value and kind == "title":
                titles.append(value)
            elif value and kind == "seniority":
                seniority.append(value)
    return list(dict.fromkeys(titles)), list(dict.fromkeys(seniority))


EXAMPLE_TITLES = ["Senior Product Manager", "Product Manager"]
EXAMPLE_SENIORITY = ["Senior", "Staff", "Principal"]


def starter_config(titles: list[str] | None = None, seniority: list[str] | None = None,
                   seeded_from: str = "") -> str:
    """The text of a starter ranking.toml."""
    def table(names: list[str], points: int) -> str:
        return "\n".join(f"{json.dumps(name)} = {points}" for name in names)
    example = "EXAMPLES ONLY: replace them with your own"
    origin = f"seeded from {seeded_from}" if seeded_from and titles else example
    seniority_origin = f"seeded from {seeded_from}" if seeded_from and seniority else example
    return f"""\
# rolescout ranking: which open roles reach your shortlist, and in what order.
#
# This file is yours: edit it freely, then run `rolescout shortlist rebuild`.
# Rebuilding reads only the local database; it makes no network call.
# It is personal data. Keep it next to the database, not in the repository.
#
# How a role is scored: it gets points from each section below and the points
# are added up. Higher scores rank first. `rolescout why ID` shows the sum
# for any role.

# How many roles the shortlist holds.
size = 100

# Points for the company's tier. A company with no tier gets none.
[tier_points]
1 = 30
2 = 20
3 = 10

[title]
# A role whose title contains any of these phrases is never listed.
# Whole words, any capitalization. Example: exclude = ["Intern", "Contract"]
exclude = []

# Title phrases you want, with points ({origin}).
# A role must match at least one of these or it is never listed.
# Whole words, any capitalization: "Product Manager" matches "Senior Product
# Manager, Growth" but not "Product Marketing Manager". If several match, the
# one worth the most points counts. Raise the points of the titles you want most.
[title.wanted]
{table(titles or EXAMPLE_TITLES, 30)}

# Seniority words in the title, with points ({seniority_origin}).
# If several match, the one worth the most counts.
[seniority.words]
{table(seniority or EXAMPLE_SENIORITY, 10)}

[location]
# Points when the role is remote (the board says so, or the location does).
remote_points = 10

# Points when the location contains one of these, e.g. "New York" = 10.
# Any capitalization. A role that is both remote and in a preferred place
# gets the larger of the two, not both.
[location.preferred]

# Newer roles first: a role first seen within `full_days` gets `max_points`,
# fading evenly to nothing by `zero_days`.
[recency]
max_points = 15
full_days = 3
zero_days = 30
"""


# --- scoring -----------------------------------------------------------------

@dataclass
class Evaluation:
    eligible: bool
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)   # what earned points
    detail: list[str] = field(default_factory=list)    # every rule, including the ones that gave nothing
    why_not: str = ""                                  # when not eligible


def _pts(points: float) -> str:
    return f"+{points:g}"


def _best(matches: list[tuple[str, float]]) -> tuple[str, float] | None:
    # Most points wins; between equals, the longer (more specific) phrase.
    return max(matches, key=lambda m: (m[1], len(m[0]))) if matches else None


def _days_since(stamp: str, today: date) -> int:
    return max(0, (today - date.fromisoformat(stamp[:10])).days)


def evaluate(role, cfg: RankingConfig, today: date) -> Evaluation:
    """Score one role. ``role`` needs title, location, workplace_type, first_seen_at, tier."""
    title = role["title"] or ""
    ev = Evaluation(eligible=True)

    def add(points: float, earned: str, nothing: str) -> None:
        if points:
            ev.score += points
            ev.reasons.append(f"{earned} ({_pts(points)})")
            ev.detail.append(f"{earned} ({_pts(points)})")
        else:
            ev.detail.append(f"{nothing} (+0)")

    excluded = next((p for p, pattern in cfg._exclude if pattern.search(title)), None)
    wanted = _best([(p, pts) for p, pts, pattern in cfg._wanted if pattern.search(title)])
    if excluded:
        ev.eligible, ev.why_not = False, f'its title contains "{excluded}", a phrase you never want'
    elif wanted is None:
        ev.eligible, ev.why_not = False, "its title matches none of the title phrases you want"

    tier = role["tier"]
    add(cfg.tier_points.get(tier, 0.0) if tier is not None else 0.0,
        f"Tier {tier} company", "Company has no tier" if tier is None else f"Tier {tier} company")
    if wanted:
        ev.score += wanted[1]
        line = f'Title matches "{wanted[0]}" ({_pts(wanted[1])})'
        ev.reasons.append(line)
        ev.detail.append(line)
    else:
        ev.detail.append("Title matches no wanted phrase (+0)")
    senior = _best([(p, pts) for p, pts, pattern in cfg._seniority if pattern.search(title)])
    add(senior[1] if senior else 0.0, f'Seniority: "{senior[0]}"' if senior else "",
        "No seniority word in the title")

    location = role["location"] or ""
    place = _best([(p, pts) for p, pts, pattern in cfg._locations if pattern.search(location)])
    remote = role["workplace_type"] == "remote" or "remote" in location.lower()
    place_points = place[1] if place else 0.0
    remote_points = cfg.remote_points if remote else 0.0
    if remote_points > place_points:
        add(remote_points, "Remote", "")
    elif place_points:
        add(place_points, f'Location matches "{place[0]}"', "")
    else:
        add(0.0, "", "Remote, but remote earns no points" if remote
            else "Not remote and not in a preferred location")

    days = _days_since(role["first_seen_at"], today)
    if days <= cfg.recency_full_days:
        share = 1.0
    elif days >= cfg.recency_zero_days:
        share = 0.0
    else:
        share = (cfg.recency_zero_days - days) / (cfg.recency_zero_days - cfg.recency_full_days)
    seen = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
    add(round(cfg.recency_max_points * share, 1), f"First seen {seen}", f"First seen {seen}")
    ev.score = round(ev.score, 1)
    return ev


# --- building and reading lists ----------------------------------------------

ROLE_SELECT = (
    "SELECT r.id, r.title, r.department, r.location, r.workplace_type, r.url, r.date_posted, "
    "r.compensation_text, r.first_seen_at, r.closed_at, c.name AS company, c.tier, c.active, "
    "m.status AS mark, m.note AS mark_note "
    "FROM roles r JOIN companies c ON c.id = r.company_id "
    "LEFT JOIN role_marks m ON m.role_id = r.id ")


def build_shortlist(conn: sqlite3.Connection, cfg: RankingConfig, *, run_id: int | None = None,
                    now: datetime | None = None) -> dict:
    """Rank every open role and store the top ``cfg.size`` as today's shortlist.

    Left out: closed roles, roles of deactivated companies, roles marked
    dismissed or applied, and roles that fail the title rules. A list built
    earlier the same local day is replaced; lists older than 30 days are dropped.
    """
    now = now or datetime.now().astimezone()
    list_date = now.date().isoformat()
    today = now.astimezone(timezone.utc).date()

    ranked = []
    for role in conn.execute(
            ROLE_SELECT + "WHERE r.closed_at IS NULL AND c.active = 1 "
            "AND (m.status IS NULL OR m.status NOT IN ('dismissed', 'applied'))"):
        ev = evaluate(role, cfg, today)
        if ev.eligible:
            ranked.append((role, ev))
    # Best score first; between equals: newer, better tier, then a fixed order.
    ranked.sort(key=lambda pair: (
        -pair[1].score, -date.fromisoformat(pair[0]["first_seen_at"][:10]).toordinal(),
        pair[0]["tier"] if pair[0]["tier"] is not None else 99,
        pair[0]["company"].lower(), pair[0]["title"].lower(), pair[0]["id"]))
    top = ranked[:cfg.size]

    cutoff = (now.date() - timedelta(days=KEEP_DAYS)).isoformat()
    with transaction(conn):
        conn.execute("DELETE FROM shortlists WHERE list_date = ? OR list_date < ?", (list_date, cutoff))
        shortlist_id = conn.execute(
            "INSERT INTO shortlists (list_date, built_at, run_id, config_hash, eligible) "
            "VALUES (?, ?, ?, ?, ?)",
            (list_date, now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), run_id,
             cfg.digest, len(ranked))).lastrowid
        conn.executemany(
            "INSERT INTO shortlist_entries (shortlist_id, rank, role_id, score, reasons) "
            "VALUES (?, ?, ?, ?, ?)",
            [(shortlist_id, rank, role["id"], ev.score, json.dumps(ev.reasons, ensure_ascii=False))
             for rank, (role, ev) in enumerate(top, start=1)])
    previous, new_ids = new_since(conn, shortlist_id)
    return {"shortlist_id": shortlist_id, "list_date": list_date, "listed": len(top),
            "eligible": len(ranked), "new": len(new_ids) if previous else None,
            "previous_date": previous["list_date"] if previous else None}


def latest_shortlist(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM shortlists ORDER BY list_date DESC LIMIT 1").fetchone()


def new_since(conn: sqlite3.Connection, shortlist_id: int, since: str | None = None):
    """(the list being compared against or None, ids of roles on this list but not on that one).

    By default the comparison is with the previous day's list. ``since``
    (YYYY-MM-DD) picks the list of that day, or the nearest earlier one.
    """
    current = conn.execute("SELECT * FROM shortlists WHERE id = ?", (shortlist_id,)).fetchone()
    bound = min(date.fromisoformat(since).isoformat(), current["list_date"]) if since else None
    previous = conn.execute(
        "SELECT * FROM shortlists WHERE list_date < ? AND (? IS NULL OR list_date <= ?) "
        "ORDER BY list_date DESC LIMIT 1", (current["list_date"], bound, bound)).fetchone()
    if previous is None:
        return None, set()
    rows = conn.execute(
        "SELECT role_id FROM shortlist_entries WHERE shortlist_id = ? AND role_id NOT IN "
        "(SELECT role_id FROM shortlist_entries WHERE shortlist_id = ?)",
        (shortlist_id, previous["id"]))
    return previous, {r["role_id"] for r in rows}


def shortlist_entries(conn: sqlite3.Connection, shortlist_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT e.rank, e.score, e.reasons, r.id, r.title, r.department, r.location, "
        "r.workplace_type, r.url, r.date_posted, r.compensation_text, r.first_seen_at, r.closed_at, "
        "c.name AS company, c.tier, m.status AS mark, m.note AS mark_note "
        "FROM shortlist_entries e JOIN roles r ON r.id = e.role_id "
        "JOIN companies c ON c.id = r.company_id LEFT JOIN role_marks m ON m.role_id = r.id "
        "WHERE e.shortlist_id = ? ORDER BY e.rank", (shortlist_id,))
    return [{**dict(r), "reasons": json.loads(r["reasons"])} for r in rows]


# --- marks -------------------------------------------------------------------

def parse_role_id(text: str) -> int:
    cleaned = str(text).strip().lstrip("#")
    if not cleaned.isdigit():
        raise MarkError(f"{text!r} is not a role ID; IDs are the numbers in the ID column")
    return int(cleaned)


def get_role(conn: sqlite3.Connection, role_id: int) -> sqlite3.Row:
    row = conn.execute(ROLE_SELECT + "WHERE r.id = ?", (role_id,)).fetchone()
    if row is None:
        raise MarkError(f"no role with ID {role_id}")
    return row


def _drop_description_if_closed(conn: sqlite3.Connection, role_id: int) -> None:
    conn.execute(
        "DELETE FROM role_descriptions WHERE role_id = ? AND role_id IN "
        "(SELECT id FROM roles WHERE closed_at IS NOT NULL) AND role_id NOT IN "
        "(SELECT role_id FROM role_marks WHERE status IN ('saved', 'applied'))", (role_id,))


def mark_role(conn: sqlite3.Connection, role_id: int, status: str, note: str = "") -> sqlite3.Row:
    """Mark a role dismissed, saved or applied. A new mark replaces the old one."""
    if status not in MARKS:
        raise MarkError(f"status must be one of {', '.join(MARKS)}, got {status!r}")
    get_role(conn, role_id)
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO role_marks (role_id, status, note, marked_at) VALUES (?, ?, ?, ?)",
            (role_id, status, note.strip(), utc_now()))
        _drop_description_if_closed(conn, role_id)  # e.g. saved -> dismissed on a closed role
    return get_role(conn, role_id)


def unmark_role(conn: sqlite3.Connection, role_id: int) -> str | None:
    """Remove a role's mark. Returns the status it had, or None if it had none."""
    role = get_role(conn, role_id)
    with transaction(conn):
        conn.execute("DELETE FROM role_marks WHERE role_id = ?", (role_id,))
        _drop_description_if_closed(conn, role_id)
    return role["mark"]


def list_marks(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT r.id, m.status, m.note, m.marked_at, c.name AS company, r.title, r.location, r.url, "
        "r.closed_at, EXISTS (SELECT 1 FROM role_descriptions d WHERE d.role_id = r.id) AS has_description "
        "FROM role_marks m JOIN roles r ON r.id = m.role_id JOIN companies c ON c.id = r.company_id "
        "WHERE ? IS NULL OR m.status = ? ORDER BY m.marked_at DESC, r.id", (status, status))
    return [dict(r) for r in rows]


# --- explaining --------------------------------------------------------------

def explain(conn: sqlite3.Connection, role_id: int, cfg: RankingConfig,
            now: datetime | None = None) -> dict:
    """Why a role ranks where it does, or why it is not on the list."""
    now = now or datetime.now().astimezone()
    role = get_role(conn, role_id)
    ev = evaluate(role, cfg, now.astimezone(timezone.utc).date())
    current = latest_shortlist(conn)
    entry = cutoff = None
    if current is not None:
        entry = conn.execute(
            "SELECT rank, score FROM shortlist_entries WHERE shortlist_id = ? AND role_id = ?",
            (current["id"], role_id)).fetchone()
        cutoff = conn.execute(
            "SELECT rank, score FROM shortlist_entries WHERE shortlist_id = ? ORDER BY rank DESC LIMIT 1",
            (current["id"],)).fetchone()

    if role["closed_at"]:
        why_not = f"the role closed on {role['closed_at'][:10]}"
    elif not role["active"]:
        why_not = f"{role['company']} is deactivated"
    elif role["mark"] in HIDDEN_MARKS:
        why_not = f"you marked it {role['mark']}"
    elif not ev.eligible:
        why_not = ev.why_not
    elif entry is None and current is not None and cutoff is not None and cutoff["rank"] >= cfg.size:
        why_not = (f"{cutoff['rank']} roles rank above it: it scores {ev.score:g} and the last "
                   f"role on the list scores {cutoff['score']:g}")
    elif entry is None:
        why_not = "the list has not been rebuilt since this changed; run: rolescout shortlist rebuild"
    else:
        why_not = ""
    return {
        "role": dict(role), "score": ev.score, "detail": ev.detail,
        "rank": entry["rank"] if entry and not why_not else None,
        "listed_score": entry["score"] if entry else None,
        "list_date": current["list_date"] if current else None,
        "list_size": cutoff["rank"] if cutoff else 0,
        "config_changed": bool(current) and current["config_hash"] != cfg.digest,
        "why_not": why_not,
    }
