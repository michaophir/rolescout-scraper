"""Coverage gaps: which companies we cannot fetch, and which connector to build next.

Everything here reads the local database. Nothing is sent anywhere.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from rolescout.adapters import ADAPTERS
from rolescout.db import get_meta, set_meta, utc_now
from rolescout.ops import failing_boards

# A job system becomes a build candidate at this many companies, or with one tier-1 company.
CANDIDATE_MIN_COMPANIES = 3
CANDIDATE_TIER = 1
CUSTOM = "custom"

RECHECK_META_KEY = "last_recheck_at"
RECHECK_SLACK = timedelta(hours=12)  # so a daily job still fires on the 7th day, not the 8th


class GapsError(Exception):
    pass


def classify(conn: sqlite3.Connection) -> list[dict]:
    """Every active company with its coverage state:

    - ``covered``        has a board that is not failing
    - ``unsupported``    on a named job system we have no adapter for
    - ``needs_mapping``  on a supported system, but no board mapped yet
    - ``unidentified``   job system unknown
    A company whose every board has failed three or more runs in a row is
    treated as not covered: a dead board usually means it moved systems.
    """
    failing: dict[str, list[dict]] = {}
    for board in failing_boards(conn):
        failing.setdefault(board["company"], []).append(board)
    board_counts = {r["company_id"]: r["n"] for r in conn.execute(
        "SELECT company_id, COUNT(*) AS n FROM boards GROUP BY company_id")}

    entries = []
    for c in conn.execute(
            "SELECT c.id, c.name, c.website, c.tier, r.outcome, r.job_system, r.job_system_url, "
            "r.careers_url, r.evidence, r.resolved_at, r.manual FROM companies c "
            "LEFT JOIN company_resolutions r ON r.company_id = c.id WHERE c.active = 1 "
            "ORDER BY c.tier IS NULL, c.tier, c.name"):
        evidence = json.loads(c["evidence"]) if c["evidence"] else {"candidates": [], "notes": []}
        pending = [x for x in evidence.get("candidates", []) if x["status"] == "unconfirmed"]
        dead = failing.get(c["name"], [])
        entry = {
            "company": c["name"], "tier": c["tier"], "website": c["website"],
            "system": c["job_system"], "url": c["job_system_url"], "careers_url": c["careers_url"],
            "manual": bool(c["manual"]), "resolved_at": c["resolved_at"],
            "notes": evidence.get("notes", []), "candidates": pending,
            "failing_boards": [f"{b['source']}/{b['ref']}" for b in dead],
        }
        boards = board_counts.get(c["id"], 0)
        system = c["job_system"]
        if boards and len(dead) < boards:
            entry["state"] = "covered"
        elif system and system not in ADAPTERS and (c["manual"] or system != CUSTOM):
            entry["state"] = "unsupported"
        elif (system in ADAPTERS or pending) and not boards:
            entry["state"] = "needs_mapping"
        else:
            entry["state"] = "unidentified"
            entry["system"] = None
        if entry["state"] == "unidentified":
            if dead:
                entry["reason"] = "its board keeps failing and no replacement was found"
            elif c["outcome"] is None:
                entry["reason"] = "not resolved yet"
            elif c["careers_url"]:
                entry["reason"] = "careers page names no job system we recognize"
            else:
                entry["reason"] = "; ".join(entry["notes"][:2]) or "no careers page or board found"
        entries.append(entry)
    return entries


def gaps_report(conn: sqlite3.Connection) -> dict:
    """Unsupported companies grouped by job system, with build candidates flagged."""
    entries = classify(conn)
    systems: dict[str, list[dict]] = {}
    for e in entries:
        if e["state"] == "unsupported":
            systems.setdefault(e["system"], []).append(e)

    groups = []
    for system, members in systems.items():
        reasons = []
        # Custom sites share no connector, so their count alone means nothing.
        if system != CUSTOM and len(members) >= CANDIDATE_MIN_COMPANIES:
            reasons.append(f"{len(members)} companies")
        tier_one = [m["company"] for m in members if m["tier"] == CANDIDATE_TIER]
        if tier_one:
            reasons.append(f"tier {CANDIDATE_TIER}: {', '.join(tier_one)}")
        groups.append({"system": system, "companies": members,
                       "build_candidate": bool(reasons), "why": "; ".join(reasons)})
    groups.sort(key=lambda g: (not g["build_candidate"], -len(g["companies"]), g["system"]))

    report = {
        "active_companies": len(entries),
        "covered": sum(e["state"] == "covered" for e in entries),
        "systems": groups,
        "needs_mapping": [e for e in entries if e["state"] == "needs_mapping"],
        "unidentified": [e for e in entries if e["state"] == "unidentified"],
    }
    report["build_candidates"] = sum(g["build_candidate"] for g in groups)
    return report


def summary_line(report: dict) -> str:
    """The one line ``rolescout status`` shows."""
    n = report["build_candidates"]
    parts = [f"{n} build candidate{'' if n == 1 else 's'}",
             f"{len(report['unidentified'])} unidentified"]
    if report["needs_mapping"]:
        parts.append(f"{len(report['needs_mapping'])} awaiting mapping")
    return ", ".join(parts)


def _tier(tier) -> str:
    return f"tier {tier}" if tier is not None else "no tier"


def connector_brief(conn: sqlite3.Connection, system: str) -> str:
    """A Markdown brief for one job system, ready to paste into a planning chat."""
    report = gaps_report(conn)
    wanted = system.strip().lower()
    group = next((g for g in report["systems"] if g["system"] == wanted), None)
    if group is None:
        known = ", ".join(g["system"] for g in report["systems"]) or "none"
        raise GapsError(f"no unsupported company is recorded on {system!r}. Systems with gaps: {known}")

    members = group["companies"]
    lines = [
        f"# Connector brief: {wanted}",
        "",
        f"rolescout cannot fetch {len(members)} target compan{'y' if len(members) == 1 else 'ies'} "
        f"because they post jobs on {wanted}, which has no adapter yet.",
        f"Build candidate: {'yes (' + group['why'] + ')' if group['build_candidate'] else 'no (below the thresholds)'}.",
        f"Generated {utc_now()} from the local database.",
        "",
        "## Affected companies",
        "",
    ]
    for m in sorted(members, key=lambda m: (m["tier"] is None, m["tier"], m["company"])):
        lines.append(f"### {m['company']} ({_tier(m['tier'])})")
        lines.append(f"- Website: {m['website'] or 'not recorded'}")
        lines.append(f"- Careers page: {m['careers_url'] or 'not recorded'}")
        lines.append(f"- Job system URL: {m['url'] or 'not recorded'}")
        lines.append(f"- Identified: {'by hand' if m['manual'] else 'automatically'}"
                     + (f", {m['resolved_at'][:10]}" if m["resolved_at"] else ""))
        for note in m["notes"]:
            lines.append(f"- Note: {note}")
        lines.append("")
    lines += [
        "## What the adapter must do",
        "",
        "One module in `src/rolescout/adapters/`, a subclass of `SourceAdapter`:",
        "- `board_url(ref)`: the one URL that returns every open role for a board reference.",
        "- `items(payload, ref)`: the raw posting objects in that response.",
        "- `parse(payload, ref, company=None)`: normalize each posting to a `Role` with source,",
        "  source_job_id, company, title, department, location, workplace_type",
        "  (remote / hybrid / onsite / blank), url, date_posted (YYYY-MM-DD), compensation_text",
        "  and description (plain text).",
        "- `identity(ref)`: the board's own company name and website, used to confirm a guessed board.",
        "",
        "## Questions to settle in the spec",
        "",
        "- What is the board reference for each company above (the part of the URL that names the tenant)?",
        "- Is there a public, unauthenticated endpoint that lists every open role in one call, with descriptions?",
        "  If descriptions need one request per role, how many roles do these companies have?",
        "- How should `companies resolve` recognize a board link for this system and extract the reference?",
        "- Rate limits, robots.txt rules or terms that constrain a once-a-day fetch.",
        "",
    ]
    return "\n".join(lines)


# --- the weekly re-check -----------------------------------------------------

def recheck_due(conn: sqlite3.Connection, every_days: float, now: datetime | None = None) -> bool:
    """Whether coverage should be re-checked now. Never checked counts as due."""
    if every_days <= 0:
        return False
    last = get_meta(conn, RECHECK_META_KEY)
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    then = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return now - then >= timedelta(days=every_days) - RECHECK_SLACK


def mark_rechecked(conn: sqlite3.Connection, now: str | None = None) -> None:
    set_meta(conn, RECHECK_META_KEY, now or utc_now())
