"""What changed since an earlier point: the previous completed scout by default.

Everything comes from the local database: the per-run company snapshots, the
run's board results, the stored shortlists, and the dates companies and
boards were added. Nothing is fetched.
"""

import sqlite3
from datetime import datetime, timezone

from rolescout.shortlist import new_between

TOP_COMPANIES = 5
MAX_NAMES = 6
MAX_SHORTLIST = 10


class DigestError(Exception):
    pass


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _since_stamp(since: str) -> str:
    """Local midnight of a YYYY-MM-DD date, as a UTC timestamp."""
    try:
        day = datetime.strptime(since.strip(), "%Y-%m-%d")
    except ValueError:
        raise DigestError(f"--since must be a date like 2026-01-31, got {since!r}") from None
    return day.astimezone().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _coverage(conn: sqlite3.Connection, run: sqlite3.Row | None, as_of: str | None) -> dict | None:
    """Coverage at a run: companies known, mapped to a board, fetched without error."""
    if run is None:
        return None
    fetched = conn.execute(
        "SELECT COUNT(*) FROM (SELECT company_id FROM run_boards WHERE run_id = ? "
        "GROUP BY company_id HAVING SUM(status != 'ok') = 0)", (run["id"],)).fetchone()[0]
    known = conn.execute(
        "SELECT COUNT(*) FROM companies WHERE active = 1 AND (? IS NULL OR created_at <= ?)",
        (as_of, as_of)).fetchone()[0]
    return {"companies": known, "mapped": run["companies_total"], "fetched_ok": fetched,
            "open_roles": run["roles_open"]}


def build_digest(conn: sqlite3.Connection, since: str | None = None, now: datetime | None = None) -> dict:
    """The digest as data. ``since`` (YYYY-MM-DD, local) compares with the
    last scout completed before that day began, instead of the previous one."""
    now = now or datetime.now(timezone.utc)
    completed = conn.execute("SELECT * FROM runs WHERE status = 'completed' ORDER BY id").fetchall()
    latest = completed[-1] if completed else None
    if since is not None:
        cut = _since_stamp(since)
        earlier = [r for r in completed if r["started_at"] < cut]
        then = earlier[-1] if earlier else None
        after = then["finished_at"] if then else cut
        if then is not None and latest is not None and then["id"] == latest["id"]:
            latest = None        # no scout has completed since that day
    else:
        then = completed[-2] if len(completed) >= 2 else None
        after = then["finished_at"] if then else None
    out: dict = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "since": since, "compared_with_run": then["started_at"] if then else None,
        "latest_run": latest["started_at"] if latest else None,
        "note": "", "roles": None, "first_fetched": [], "shortlist": None,
        "companies": {"added": [], "mapped": [], "newly_failing": []}, "coverage": {"now": None, "then": None},
    }
    if latest is None:
        out["note"] = ("No scout has completed since then." if completed else "No scout has completed yet.")
        if not completed:
            return out
    elif then is None and since is None:
        out["note"] = "This was the first completed scout, so there is nothing earlier to compare with."

    # Every run after the comparison point counts, an interrupted one included:
    # what it stored is real.
    first_id = then["id"] if then else 0
    last_id = latest["id"] if latest else first_id
    window = (first_id, last_id)

    per_company = conn.execute(
        "SELECT c.name, SUM(s.opened) AS opened, SUM(s.closed) AS closed, MAX(s.baseline) AS baseline "
        "FROM company_snapshots s JOIN companies c ON c.id = s.company_id "
        "WHERE s.run_id > ? AND s.run_id <= ? GROUP BY s.company_id", window).fetchall()

    def top(key: str) -> list[dict]:
        rows = sorted((r for r in per_company if r[key]), key=lambda r: (-r[key], r["name"].lower()))
        return [{"company": r["name"], "count": r[key]} for r in rows[:TOP_COMPANIES]]
    out["roles"] = {
        "opened": sum(r["opened"] for r in per_company), "closed": sum(r["closed"] for r in per_company),
        "top_opened": top("opened"), "top_closed": top("closed"),
        "open_now": latest["roles_open"] if latest else None,
        "open_then": then["roles_open"] if then else None}

    out["first_fetched"] = [
        {"company": r["name"], "open_roles": r["roles"] or 0} for r in conn.execute(
            "SELECT c.name, SUM(rb.roles_fetched) AS roles FROM run_boards rb "
            "JOIN companies c ON c.id = rb.company_id "
            "WHERE rb.run_id > ? AND rb.run_id <= ? AND rb.baseline = 1 AND rb.status = 'ok' "
            # A company's first fetch, not a second board added to one already fetched.
            "AND NOT EXISTS (SELECT 1 FROM run_boards e WHERE e.company_id = rb.company_id "
            "                AND e.run_id <= ? AND e.status = 'ok') "
            "GROUP BY rb.company_id ORDER BY roles DESC, c.name", (*window, first_id))]

    # Shortlist: the current list against the last one built by the comparison point.
    current = conn.execute("SELECT * FROM shortlists ORDER BY list_date DESC LIMIT 1").fetchone()
    if current is not None:
        before = None
        if then is not None or since is not None:
            # The list that followed the comparison scout, or the last one built before the date.
            before = conn.execute(
                "SELECT * FROM shortlists WHERE id != ? AND (built_at <= ? OR (run_id IS NOT NULL AND run_id <= ?)) "
                "ORDER BY list_date DESC LIMIT 1", (current["id"], after, first_id)).fetchone()
        # An entry is one job; it is not new when the earlier list showed
        # the same job under another city's posting.
        new_ids = set() if before is None else new_between(conn, current["id"], before["id"])
        new_entries = [e for e in conn.execute(
            "SELECT e.rank, c.name AS company, r.title, r.id AS role_id FROM shortlist_entries e "
            "JOIN roles r ON r.id = e.role_id JOIN companies c ON c.id = r.company_id "
            "WHERE e.shortlist_id = ? ORDER BY e.rank", (current["id"],)) if e["role_id"] in new_ids]
        size = conn.execute("SELECT COUNT(*) FROM shortlist_entries WHERE shortlist_id = ?",
                            (current["id"],)).fetchone()[0]
        out["shortlist"] = {
            "list_date": current["list_date"], "size": size,
            "compared_with": before["list_date"] if before else None,
            "new_count": len(new_entries),
            "new": [dict(e) for e in new_entries[:MAX_SHORTLIST]]}

    if after is not None:
        out["companies"]["added"] = [r["name"] for r in conn.execute(
            "SELECT name FROM companies WHERE created_at > ? ORDER BY created_at, name", (after,))]
        out["companies"]["mapped"] = [r["name"] for r in conn.execute(
            "SELECT DISTINCT c.name FROM boards b JOIN companies c ON c.id = b.company_id "
            "WHERE b.mapped_at > ? ORDER BY c.name", (after,))]
    if latest is not None and then is not None:
        # Failed in the latest scout, and was fine (or not there) at the comparison point.
        out["companies"]["newly_failing"] = [
            {"company": r["name"], "board": f"{r['source']}/{r['ref']}", "error": r["error"] or ""}
            for r in conn.execute(
                "SELECT c.name, rb.source, rb.ref, rb.error FROM run_boards rb "
                "JOIN companies c ON c.id = rb.company_id WHERE rb.run_id = ? AND rb.status != 'ok' "
                "AND NOT EXISTS (SELECT 1 FROM run_boards p WHERE p.run_id = ? AND p.source = rb.source "
                "                AND p.ref = rb.ref AND p.status != 'ok') ORDER BY c.name",
                (latest["id"], then["id"]))]

    out["coverage"] = {"now": _coverage(conn, latest or then, None),
                       "then": _coverage(conn, then, then["finished_at"] if then else None)}
    return out


def _when(stamp: str | None) -> str:
    if not stamp:
        return "-"
    when = _parse(stamp).astimezone()
    return f"{when:%a} {when.day} {when:%b} {when:%H:%M}"


def _names(names: list[str]) -> str:
    more = len(names) - MAX_NAMES
    return ", ".join(names[:MAX_NAMES]) + (f", +{more} more" if more > 0 else "")


def digest_text(d: dict) -> str:
    """The digest as a few short lines, readable on a phone."""
    if d["compared_with_run"]:
        basis = f"since the scout of {_when(d['compared_with_run'])}"
    elif d["since"]:
        basis = f"since {d['since']} (no scout had completed before then)"
    else:
        basis = "nothing earlier to compare with"
    lines = [f"rolescout digest, {_when(d['generated_at'])}: {basis}"]
    if d["latest_run"]:
        lines.append(f"Latest scout: {_when(d['latest_run'])}")
    if d["note"]:
        lines.append(d["note"])
    roles = d["roles"]
    if roles is None:
        return "\n".join(lines)

    def was(now, then) -> str:
        return f"{now:,}" + (f" (was {then:,})" if then is not None and then != now else "")
    open_now = f"; open now {was(roles['open_now'], roles['open_then'])}" if roles["open_now"] is not None else ""
    lines.append(f"Roles: {roles['opened']:,} opened, {roles['closed']:,} closed{open_now}")
    for label, key in (("Opened most", "top_opened"), ("Closed most", "top_closed")):
        if roles[key]:
            lines.append(f"  {label}: " + ", ".join(f"{t['company']} {t['count']}" for t in roles[key]))
    first = d["first_fetched"]
    if first:
        lines.append(f"Fetched for the first time ({len(first)}): "
                     + _names([f"{f['company']} ({f['open_roles']})" for f in first]))
    shortlist = d["shortlist"]
    if shortlist is None:
        lines.append("Shortlist: none built yet")
    elif shortlist["compared_with"] is None and not shortlist["new_count"]:
        lines.append(f"Shortlist: {shortlist['size']} roles; no earlier list to compare with")
    else:
        lines.append(f"Shortlist: {shortlist['new_count']} new of {shortlist['size']}"
                     + (f" (against the list of {shortlist['compared_with']})" if shortlist["compared_with"] else ""))
        for entry in shortlist["new"]:
            lines.append(f"  #{entry['rank']} {entry['company']}: {entry['title']}")
        if shortlist["new_count"] > len(shortlist["new"]):
            lines.append(f"  +{shortlist['new_count'] - len(shortlist['new'])} more: rolescout shortlist --new")
    companies = d["companies"]
    lines.append(f"Companies: {len(companies['added'])} added, {len(companies['mapped'])} mapped, "
                 f"{len(companies['newly_failing'])} newly failing")
    if companies["added"]:
        lines.append("  Added: " + _names(companies["added"]))
    if companies["mapped"]:
        lines.append("  Mapped: " + _names(companies["mapped"]))
    for failing in companies["newly_failing"][:MAX_NAMES]:
        lines.append(f"  Failing: {failing['company']} ({failing['board']}): {failing['error'][:80]}")
    if len(companies["newly_failing"]) > MAX_NAMES:
        lines.append(f"  +{len(companies['newly_failing']) - MAX_NAMES} more failing: rolescout status")
    cov, then = d["coverage"]["now"], d["coverage"]["then"]
    if cov:
        def pair(key: str) -> str:
            return was(cov[key], then[key] if then else None)
        lines.append(f"Coverage: {pair('mapped')} of {pair('companies')} companies mapped; "
                     f"{pair('fetched_ok')} fetched without error")
    return "\n".join(lines)
