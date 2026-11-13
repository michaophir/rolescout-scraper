"""Candidates: companies found on portfolio boards, filtered by a config file.

Every candidate ends up eligible, needing review, or rejected, with the
reason. The rules live in one TOML file next to the database. Applying them
reads only the database, so a config change can be re-applied with no
network. The one rule that costs a request (does the company have an open
role in the wanted locations) is answered during a harvest and its answer is
stored, so it too is re-applied offline.
"""

import csv
import hashlib
import json
import os
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from rolescout.companies import CompanyError, add_company
from rolescout.db import transaction, utc_now
from rolescout.resolve import registrable_domain

CONFIG_NAME = "candidates.toml"
CONFIG_ENV_VAR = "ROLESCOUT_CANDIDATES"
ACTIVATION_TIER = 3
STATUSES = ("eligible", "review", "rejected", "tracked", "dismissed")
RULES = ("size", "stage", "sector", "roles")
UNKNOWN_CHOICES = ("review", "reject", "pass")


class ConfigError(Exception):
    """The candidates filter config is missing or not usable."""


class CandidateError(Exception):
    pass


# --- the config --------------------------------------------------------------

def config_path(db_path: str | os.PathLike, flag: str | os.PathLike | None = None) -> Path:
    """--config flag, then $ROLESCOUT_CANDIDATES, then candidates.toml next to the database."""
    chosen = flag or os.environ.get(CONFIG_ENV_VAR)
    return Path(chosen).expanduser() if chosen else Path(db_path).parent / CONFIG_NAME


@dataclass
class FilterConfig:
    min_employees: int = 0
    size_unknown: str = "review"
    stage_accept: list[str] = field(default_factory=list)
    stage_reject: list[str] = field(default_factory=list)
    sector_keywords: list[str] = field(default_factory=list)
    sector_unknown: str = "review"
    require_open_role: bool = False
    role_locations: list[str] = field(default_factory=list)
    source: str = ""
    digest: str = ""

    def __post_init__(self) -> None:
        self._keywords = [(k, _keyword(k)) for k in self.sector_keywords]

    @property
    def roles_rule(self) -> str:
        return json.dumps(sorted(self.role_locations))


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _keyword(keyword: str) -> re.Pattern:
    """Whole words, any case, spacing ignored: "healthcare" matches "Health Care",
    "AI" matches "Artificial Intelligence (AI)" but not "Retail"."""
    forms = {r"\s+".join(re.escape(w) for w in keyword.split()), re.escape(_squash(keyword))}
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(sorted(forms)) + r")(?![A-Za-z0-9])", re.I)


def _table(raw: dict, name: str, allowed: set[str]) -> dict:
    section = raw.get(name) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a table")
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(f"unknown setting {', '.join(unknown)} in [{name}]; expected {', '.join(sorted(allowed))}")
    return section


def _words(section: dict, key: str, where: str) -> list[str]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise ConfigError(f"{where}.{key} must be a list of words")
    return [x.strip() for x in value]


def _unknown(section: dict, where: str) -> str:
    value = section.get("unknown", "review")
    if value not in UNKNOWN_CHOICES:
        raise ConfigError(f'{where}.unknown must be one of {", ".join(UNKNOWN_CHOICES)}, got {value!r}')
    return value


def parse_config(text: str, source: str = "") -> FilterConfig:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source or 'candidates config'} is not valid TOML: {exc}") from None
    unknown = sorted(set(raw) - {"size", "stage", "sector", "roles"})
    if unknown:
        raise ConfigError(f"unknown section {', '.join(unknown)}; expected size, stage, sector, roles")
    size = _table(raw, "size", {"min_employees", "unknown"})
    stage = _table(raw, "stage", {"accept", "reject"})
    sector = _table(raw, "sector", {"keywords", "unknown"})
    roles = _table(raw, "roles", {"require_open_role", "locations"})
    minimum = size.get("min_employees", 0)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        raise ConfigError(f"size.min_employees must be a whole number, got {minimum!r}")
    require = roles.get("require_open_role", False)
    if not isinstance(require, bool):
        raise ConfigError("roles.require_open_role must be true or false")
    locations = _words(roles, "locations", "roles")
    if require and not locations:
        raise ConfigError("roles.locations must name at least one location when require_open_role is true")
    accept, reject = (_words(stage, key, "stage") for key in ("accept", "reject"))
    both = sorted({s.lower() for s in accept} & {s.lower() for s in reject})
    if both:
        raise ConfigError(f"stage {', '.join(both)} is in both accept and reject")
    return FilterConfig(
        min_employees=minimum, size_unknown=_unknown(size, "size"),
        stage_accept=[s.lower() for s in accept], stage_reject=[s.lower() for s in reject],
        sector_keywords=_words(sector, "keywords", "sector"), sector_unknown=_unknown(sector, "sector"),
        require_open_role=require, role_locations=locations,
        source=source, digest=hashlib.sha256(text.encode()).hexdigest()[:12])


def load_config(path: str | os.PathLike) -> FilterConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"no candidates filter config at {path}. Create one with: rolescout candidates init")
    return parse_config(path.read_text(encoding="utf-8"), str(path))


def read_sector_keywords(path: str | os.PathLike) -> list[str]:
    """Sector keywords from the ``domain`` rows of a role_filters.csv-style file (field,value)."""
    found = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if (row.get("field") or "").strip().lower() == "domain" and (row.get("value") or "").strip():
                found.append(row["value"].strip())
    return list(dict.fromkeys(found))


EXAMPLE_KEYWORDS = ["software", "data"]


def starter_config(keywords: list[str] | None = None, seeded_from: str = "") -> str:
    """The text of a starter candidates.toml."""
    origin = (f"seeded from the domain rows of {seeded_from}" if seeded_from and keywords
              else "EXAMPLES ONLY: replace them with the sectors you want")
    words = ", ".join(json.dumps(k) for k in (keywords or EXAMPLE_KEYWORDS))
    return f"""\
# rolescout candidates filter: which companies found on portfolio boards are
# worth tracking.
#
# This file is yours: edit it, then run `rolescout candidates apply`. Applying
# reads only the local database; it makes no network call. It is personal
# data. Keep it next to the database, not in the repository.
#
# A candidate is checked against the sections below in order. The first rule
# it fails rejects it. A rule that cannot be decided sends it to review. A
# candidate that passes every rule is eligible. `rolescout candidates why NAME`
# shows each rule's result.

[size]
# Smallest company worth tracking, by the board's employee count.
min_employees = 10
# When the board gives no employee count: "review", "reject" or "pass".
unknown = "review"

[stage]
# Funding stages, as the boards name them. A stage in `accept` passes, one in
# `reject` is rejected, and anything else (no stage, "other", a name not
# listed here) goes to review.
accept = [
    "seed", "series_a", "series_b", "series_c", "series_d", "series_e", "series_f",
    "series_g", "series_h", "series_unknown", "private_equity", "acquisition", "ipo",
]
reject = ["pre_seed"]

[sector]
# The company's sector tags must contain at least one of these
# ({origin}).
# Whole words, any capitalization, spacing ignored: "healthcare" matches
# "Health Care"; "AI" matches "Artificial Intelligence (AI)" but not "Retail".
keywords = [{words}]
# When the board gives no sector tags at all: "review", "reject" or "pass".
unknown = "review"

[roles]
# Require at least one open role in one of `locations`. This is checked last
# and only for candidates that passed everything above, because it costs one
# request per company during a harvest. Only the number of matching roles is
# stored, never the roles.
require_open_role = true
locations = ["United States", "Remote"]
"""


# --- the rules ---------------------------------------------------------------

@dataclass
class Verdict:
    status: str                      # eligible | review | rejected
    reason: str
    checks: dict[str, dict]          # rule -> {"result", "detail"}
    needs_role_check: bool = False


def _check(result: str, detail: str) -> dict:
    return {"result": result, "detail": detail}


def _by_unknown(choice: str, detail: str) -> dict:
    return _check({"review": "review", "reject": "reject", "pass": "pass"}[choice], detail)


def evaluate(candidate, cfg: FilterConfig) -> Verdict:
    """Apply the rules to one candidate. ``candidate`` needs employees, stage,
    sectors (list), matching_roles and roles_rule."""
    checks: dict[str, dict] = {}

    employees = candidate["employees"]
    if employees is None:
        checks["size"] = _by_unknown(cfg.size_unknown, "the board gives no employee count")
    elif employees >= cfg.min_employees:
        checks["size"] = _check("pass", f"{employees} employees (minimum {cfg.min_employees})")
    else:
        checks["size"] = _check("reject", f"{employees} employees, below the minimum of {cfg.min_employees}")

    stage = (candidate["stage"] or "").lower()
    if stage in cfg.stage_accept:
        checks["stage"] = _check("pass", f"stage {stage}")
    elif stage in cfg.stage_reject:
        checks["stage"] = _check("reject", f"stage {stage} is one you reject")
    elif not cfg.stage_accept and not cfg.stage_reject:
        checks["stage"] = _check("pass", "no stage rule set")
    else:
        checks["stage"] = _check("review", f"stage is {'not given' if not stage else repr(stage)}, "
                                           "neither accepted nor rejected by the config")

    sectors = candidate["sectors"]
    if not cfg.sector_keywords:
        checks["sector"] = _check("pass", "no sector rule set")
    elif not sectors:
        checks["sector"] = _by_unknown(cfg.sector_unknown, "the board gives no sector tags")
    else:
        hit = next(((kw, tag) for tag in sectors for kw, pattern in cfg._keywords
                    if pattern.search(tag) or pattern.search(_squash(tag))), None)
        checks["sector"] = (_check("pass", f'sector "{hit[1]}" matches "{hit[0]}"') if hit else
                            _check("reject", "none of its sectors match your keywords "
                                             f"({', '.join(sectors[:4])}{', ...' if len(sectors) > 4 else ''})"))

    results = [checks[rule]["result"] for rule in ("size", "stage", "sector")]
    where = " or ".join(cfg.role_locations)
    if "reject" in results:
        failed = next(rule for rule in ("size", "stage", "sector") if checks[rule]["result"] == "reject")
        checks["roles"] = _check("skipped", "not checked: already rejected")
        return Verdict("rejected", checks[failed]["detail"], checks)
    if "review" in results:
        checks["roles"] = _check("skipped", "not checked until the review is settled")
        reasons = [checks[rule]["detail"] for rule in ("size", "stage", "sector")
                   if checks[rule]["result"] == "review"]
        return Verdict("review", "; ".join(reasons), checks)
    if not cfg.require_open_role:
        checks["roles"] = _check("pass", "no open-role rule set")
        return Verdict("eligible", "passes every rule", checks)
    count = candidate["matching_roles"]
    if count is None or candidate["roles_rule"] != cfg.roles_rule:
        checks["roles"] = _check("pending", f"open roles in {where} not counted yet; the next harvest asks")
        return Verdict("review", checks["roles"]["detail"], checks, needs_role_check=True)
    if count > 0:
        checks["roles"] = _check("pass", f"{count} open role{'' if count == 1 else 's'} in {where}")
        return Verdict("eligible", "passes every rule", checks)
    checks["roles"] = _check("reject", f"no open role in {where}")
    return Verdict("rejected", checks["roles"]["detail"], checks)


def link_tracked(conn: sqlite3.Connection) -> int:
    """Link every candidate whose domain is a tracked company's website domain.
    Must run inside a transaction. Returns how many are linked."""
    tracked = {}
    for row in conn.execute("SELECT id, website FROM companies WHERE website != '' ORDER BY id"):
        tracked.setdefault(registrable_domain(row["website"]), row["id"])
    for cand in conn.execute("SELECT id, domain, company_id FROM candidates").fetchall():
        company_id = tracked.get(cand["domain"])
        if company_id is not None and cand["company_id"] != company_id:
            conn.execute("UPDATE candidates SET company_id = ? WHERE id = ?", (company_id, cand["id"]))
    return conn.execute("SELECT COUNT(*) FROM candidates WHERE company_id IS NOT NULL").fetchone()[0]


def apply_filter(conn: sqlite3.Connection, cfg: FilterConfig) -> list[int]:
    """Give every candidate its status from the config. No network.

    Returns the ids of candidates that passed every other rule and still need
    their open roles counted (a harvest does that).
    """
    pending = []
    with transaction(conn):
        link_tracked(conn)
        for cand in conn.execute("SELECT * FROM candidates ORDER BY id").fetchall():
            verdict = evaluate({**dict(cand), "sectors": json.loads(cand["sectors"])}, cfg)
            conn.execute("UPDATE candidates SET status = ?, reason = ?, checks = ? WHERE id = ?",
                         (verdict.status, verdict.reason, json.dumps(verdict.checks, ensure_ascii=False),
                          cand["id"]))
            if verdict.needs_role_check and cand["company_id"] is None and cand["dismissed_at"] is None:
                pending.append(cand["id"])
    return pending


# --- reading -----------------------------------------------------------------

SELECT = (
    "SELECT c.*, co.name AS tracked_as, "
    "CASE WHEN c.company_id IS NOT NULL THEN 'tracked' WHEN c.dismissed_at IS NOT NULL THEN 'dismissed' "
    "ELSE c.status END AS state, "
    "(SELECT COUNT(*) FROM candidate_listings l WHERE l.candidate_id = c.id) AS board_count "
    "FROM candidates c LEFT JOIN companies co ON co.id = c.company_id ")


def _boards(conn: sqlite3.Connection, candidate_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT b.id, b.name, b.url, l.slug FROM candidate_listings l JOIN portfolio_boards b "
        "ON b.id = l.board_id WHERE l.candidate_id = ? ORDER BY b.id", (candidate_id,))
    return [dict(r) for r in rows]


def _shape(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    out = dict(row)
    out["sectors"], out["locations"] = json.loads(row["sectors"]), json.loads(row["locations"])
    out["checks"] = json.loads(row["checks"])
    out["boards"] = _boards(conn, row["id"])
    return out


def list_candidates(conn: sqlite3.Connection, state: str | None = None) -> list[dict]:
    """Candidates, optionally only those in one state (eligible, review,
    rejected, tracked, dismissed). Most boards first, then largest."""
    if state is not None and state not in STATUSES:
        raise CandidateError(f"status must be one of {', '.join(STATUSES)}, got {state!r}")
    rows = conn.execute(
        f"SELECT * FROM ({SELECT}) WHERE ? IS NULL OR state = ? "
        "ORDER BY board_count DESC, employees IS NULL, employees DESC, name COLLATE NOCASE, id",
        (state, state)).fetchall()
    return [_shape(conn, r) for r in rows]


def get_candidate(conn: sqlite3.Connection, ref: str) -> dict:
    """A candidate by website domain or by name."""
    ref = str(ref).strip()
    row = conn.execute(SELECT + "WHERE c.domain = ?", (registrable_domain(ref) or ref.lower(),)).fetchone()
    if row is None:
        rows = conn.execute(SELECT + "WHERE c.name = ? COLLATE NOCASE", (ref,)).fetchall()
        if len(rows) > 1:
            raise CandidateError(f"{len(rows)} candidates are named {ref!r}; use the domain: "
                                 + ", ".join(r["domain"] for r in rows))
        row = rows[0] if rows else None
    if row is None:
        raise CandidateError(f"no candidate named {ref!r} (names and domains are in: rolescout candidates)")
    return _shape(conn, row)


def funnel(conn: sqlite3.Connection) -> dict:
    """How the candidates narrow, rule by rule. Tracked and dismissed ones
    are counted apart and take no part in the rules."""
    rows = [(_shape(conn, r)) for r in conn.execute(SELECT)]
    open_rows = [r for r in rows if r["state"] not in ("tracked", "dismissed")]

    def passed(upto: str) -> int:
        rules = RULES[:RULES.index(upto) + 1]
        return sum(all(r["checks"].get(rule, {}).get("result") == "pass" for rule in rules) for r in open_rows)
    return {
        "listed": len(rows),
        "already_tracked": sum(r["state"] == "tracked" for r in rows),
        "dismissed": sum(r["state"] == "dismissed" for r in rows),
        "considered": len(open_rows),
        "passed_size": passed("size"), "passed_stage": passed("stage"),
        "passed_sector": passed("sector"), "passed_roles": passed("roles"),
        "eligible": sum(r["state"] == "eligible" for r in open_rows),
        "needs_review": sum(r["state"] == "review" and r["checks"].get("roles", {}).get("result") != "pending"
                            for r in open_rows),
        "awaiting_role_check": sum(r["checks"].get("roles", {}).get("result") == "pending" for r in open_rows),
        "rejected": sum(r["state"] == "rejected" for r in open_rows),
    }


# --- acting ------------------------------------------------------------------

def _tag(board: dict) -> str:
    label = re.sub(r"[^a-z0-9]+", "-", (board["name"] or board["url"].split("//")[-1]).lower()).strip("-")
    return f"portfolio:{label}"


def activate(conn: sqlite3.Connection, candidate: dict, *, force: bool = False) -> dict:
    """Start tracking a candidate: add it as a tier 3 company tagged with the
    boards that list it. Finding its own job board is left to
    ``companies resolve``; nothing is fetched here."""
    state = candidate["state"]
    if state == "tracked":
        raise CandidateError(f"{candidate['name']} is already tracked as {candidate['tracked_as']!r}")
    if state in ("rejected", "dismissed") and not force:
        raise CandidateError(f"{candidate['name']} is {state} ({candidate['reason'] or candidate['dismiss_note'] or 'no reason recorded'}); "
                             "pass --force to activate it anyway")
    tags = [_tag(b) for b in candidate["boards"]]
    try:
        company_id = add_company(conn, candidate["name"], website=candidate["website"] or candidate["domain"],
                                 tier=ACTIVATION_TIER, tags=tags)
    except CompanyError as exc:
        raise CandidateError(f"{candidate['name']}: {exc}") from None
    with transaction(conn):
        conn.execute("UPDATE candidates SET company_id = ?, activated_at = ?, dismissed_at = NULL WHERE id = ?",
                     (company_id, utc_now(), candidate["id"]))
    return {"name": candidate["name"], "domain": candidate["domain"], "tier": ACTIVATION_TIER, "tags": tags}


def dismiss(conn: sqlite3.Connection, candidate: dict, note: str = "") -> None:
    """Never suggest this candidate again. Harvests keep it and keep it dismissed."""
    if candidate["state"] == "tracked":
        raise CandidateError(f"{candidate['name']} is already tracked; deactivate the company instead")
    with transaction(conn):
        conn.execute("UPDATE candidates SET dismissed_at = ?, dismiss_note = ? WHERE id = ?",
                     (utc_now(), note.strip(), candidate["id"]))


def restore(conn: sqlite3.Connection, candidate: dict) -> bool:
    """Undo a dismissal. Returns False if it was not dismissed."""
    with transaction(conn):
        return conn.execute("UPDATE candidates SET dismissed_at = NULL, dismiss_note = '' "
                            "WHERE id = ? AND dismissed_at IS NOT NULL", (candidate["id"],)).rowcount > 0
