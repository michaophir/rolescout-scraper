"""Hiring trends from the per-company snapshots each scout writes. Local data only."""

import json
import sqlite3
from datetime import date, timedelta

DEFAULT_DAYS = 30
TOP_DEPARTMENTS = 15


def _departments(first: dict[str, int], last: dict[str, int], top: int = TOP_DEPARTMENTS) -> list[dict]:
    rows = [{"department": name, "open_start": first.get(name, 0), "open_end": last.get(name, 0),
             "change": last.get(name, 0) - first.get(name, 0)}
            for name in sorted(set(first) | set(last))]
    rows.sort(key=lambda r: (-r["open_end"], -abs(r["change"]), r["department"]))
    return rows[:top]


def hiring_trends(conn: sqlite3.Connection, *, company: str | None = None, start: str | None = None,
                  end: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    """Open-role counts and roles opened and closed between two dates.

    Built from the snapshot each scout stores per company. ``opened`` leaves
    out baseline snapshots (a board's first fetch, where every role would
    look new). Department figures compare the first and last snapshot in the
    range; snapshots do not record which department an opened or closed role
    belonged to, so per-department numbers are open counts and their change.
    """
    end_date = date.fromisoformat(end) if end else date.today()
    start_date = date.fromisoformat(start) if start else end_date - timedelta(days=DEFAULT_DAYS)
    if start_date > end_date:
        raise ValueError("start must not be after end")
    params: list = [start_date.isoformat(), (end_date + timedelta(days=1)).isoformat()]
    where = "s.taken_at >= ? AND s.taken_at < ?"
    if company:
        owner = conn.execute("SELECT name FROM companies WHERE name = ?", (company.strip(),)).fetchone()
        if owner is None:
            raise ValueError(f"no company named {company!r}")
        where += " AND c.name = ?"
        params.append(owner["name"])

    per_company: dict[str, dict] = {}
    for s in conn.execute(
            "SELECT c.name, c.tier, s.taken_at, s.baseline, s.open_roles, s.opened, s.closed, "
            "s.open_by_department FROM company_snapshots s JOIN companies c ON c.id = s.company_id "
            f"WHERE {where} ORDER BY s.taken_at, s.run_id", params):
        entry = per_company.setdefault(s["name"], {
            "company": s["name"], "tier": s["tier"], "snapshots": 0, "baseline_in_range": False,
            "first_snapshot": s["taken_at"][:10], "open_start": s["open_roles"],
            "opened": 0, "closed": 0, "_first": json.loads(s["open_by_department"])})
        entry["snapshots"] += 1
        entry["last_snapshot"] = s["taken_at"][:10]
        entry["open_end"] = s["open_roles"]
        entry["closed"] += s["closed"]
        if s["baseline"]:
            entry["baseline_in_range"] = True
        else:
            entry["opened"] += s["opened"]
        entry["_last"] = json.loads(s["open_by_department"])

    companies = list(per_company.values())
    for entry in companies:
        entry["net_change"] = entry["open_end"] - entry["open_start"]
    result: dict = {
        "start": start_date.isoformat(), "end": end_date.isoformat(),
        "companies_with_data": len(companies),
        "note": ("opened excludes baseline snapshots (a company's first fetch). Department rows "
                 "compare open counts at the first and last snapshot in the range."),
    }
    if not companies:
        result["note"] = "No snapshots in this date range. Snapshots are written by each scout run."
        return {**result, "totals": None, "by_department": [], "companies": []}

    first_all: dict[str, int] = {}
    last_all: dict[str, int] = {}
    for entry in companies:
        for name, count in entry["_first"].items():
            first_all[name] = first_all.get(name, 0) + count
        for name, count in entry["_last"].items():
            last_all[name] = last_all.get(name, 0) + count
    result["totals"] = {key: sum(e[key] for e in companies)
                        for key in ("open_start", "open_end", "net_change", "opened", "closed")}

    if company:
        entry = companies[0]
        result["company"] = {k: v for k, v in entry.items() if not k.startswith("_")}
        result["by_department"] = _departments(entry["_first"], entry["_last"])
        return result

    result["by_department"] = _departments(first_all, last_all)
    companies.sort(key=lambda e: (-e["opened"], -e["open_end"], e["company"]))
    page = companies[max(0, offset):max(0, offset) + max(1, limit)]
    result["companies"] = [{k: v for k, v in e.items() if not k.startswith("_")} for e in page]
    result["next_offset"] = offset + len(page) if offset + len(page) < len(companies) else None
    return result
