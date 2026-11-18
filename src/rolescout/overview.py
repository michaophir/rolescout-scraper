"""One company at a glance: profile, VCs, its best open roles and its recent hiring."""

import sqlite3
from datetime import datetime, timedelta, timezone

from rolescout import profiles, vcs
from rolescout import shortlist as ranking
from rolescout.companies import get_company


def company_overview(conn: sqlite3.Connection, name: str, cfg=None, *, top: int = 5,
                     now: datetime | None = None) -> dict:
    """Profile, VCs, the top ``top`` open roles and the 30-day hiring trend of
    one company. With a ranking config the roles are in ranked order, the
    ones that qualify for the shortlist first; without one, newest first."""
    now = now or datetime.now(timezone.utc)
    company = get_company(conn, name)
    profile = profiles.get_profile(conn, company["name"])
    links = vcs.company_links(conn, company["id"])
    open_count = conn.execute("SELECT COUNT(*) FROM roles WHERE company_id = ? AND closed_at IS NULL",
                              (company["id"],)).fetchone()[0]

    def shaped(role, **extra) -> dict:
        return {"role_id": role["id"], "title": role["title"], "location": role["location"],
                "date_posted": role["date_posted"] or None, "first_seen": role["first_seen_at"][:10],
                "url": role["url"], "mark": role["mark"], **extra}
    if cfg is not None:
        ranked = ranking.rank_roles(conn, cfg, now, company_id=company["id"], qualifying_only=False)
        roles = [shaped(role, score=ev.score, qualifies=ev.eligible) for role, ev in ranked[:top]]
        ranked_by = "your ranking rules; roles that qualify for the shortlist first"
    else:
        rows = conn.execute(
            ranking.ROLE_SELECT + "WHERE r.closed_at IS NULL AND r.company_id = ? "
            "ORDER BY COALESCE(NULLIF(r.date_posted, ''), r.first_seen_at) DESC, r.id LIMIT ?",
            (company["id"], top)).fetchall()
        roles = [shaped(role, score=None, qualifies=None) for role in rows]
        ranked_by = "newest first (no ranking config)"

    since = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    totals = conn.execute(
        "SELECT COALESCE(SUM(opened), 0) AS opened, COALESCE(SUM(closed), 0) AS closed, COUNT(*) AS scouts "
        "FROM company_snapshots WHERE company_id = ? AND taken_at >= ?", (company["id"], since)).fetchone()
    weeks = [dict(r) for r in conn.execute(
        "SELECT week_start, opened, closed, net, open_roles_at_week_end AS open_roles FROM v_hiring_weekly "
        "WHERE company_id = ? AND week_start >= date(?, '-6 days') ORDER BY week_start", (company["id"], since[:10]))]
    return {
        "company": company["name"], "website": company["website"] or None, "active": bool(company["active"]),
        "job_boards": [f"{b['source']}/{b['ref']}" for b in conn.execute(
            "SELECT source, ref FROM boards WHERE company_id = ? ORDER BY id", (company["id"],))],
        "profile": {f: profile[f] for f in profiles.FIELDS},
        "backed_by": [l["name"] for l in links if l["type"] == "vc"],
        "ecosystem_groups": [l["name"] for l in links if l["type"] == "ecosystem"],
        "open_roles": open_count, "top_roles": roles, "top_roles_ranked_by": ranked_by,
        "hiring_30_days": {"opened": totals["opened"], "closed": totals["closed"],
                           "net": totals["opened"] - totals["closed"], "scouts": totals["scouts"], "weeks": weeks},
    }
