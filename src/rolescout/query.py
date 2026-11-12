"""Read-only questions answered from the database. No network."""

import sqlite3
from datetime import date


def _like(phrase: str) -> str:
    escaped = phrase.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def query_roles(
    conn: sqlite3.Connection,
    *,
    company: str | None = None,
    title: list[str] | None = None,
    status: str = "open",          # open | closed | all
    since: str | None = None,      # YYYY-MM-DD: first seen on or after this date
    include_inactive: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Roles matching every given filter, ordered by company then title.

    ``title`` is a list of phrases; a role matches if its title contains any
    one of them (case-insensitive).

    Open roles of deactivated companies are left out unless
    ``include_inactive`` is set: nothing refreshes them, so "open" is stale.
    Their closed roles are history and always included.
    """
    where, params = [], []
    if not include_inactive:
        where.append("(c.active = 1 OR r.closed_at IS NOT NULL)")
    if company:
        where.append("c.name = ?")
        params.append(company.strip())
    phrases = [p.strip() for p in title or [] if p.strip()]
    if phrases:
        where.append("(" + " OR ".join(["r.title LIKE ? ESCAPE '\\'"] * len(phrases)) + ")")
        params.extend(_like(p) for p in phrases)
    if status == "open":
        where.append("r.closed_at IS NULL")
    elif status == "closed":
        where.append("r.closed_at IS NOT NULL")
    elif status != "all":
        raise ValueError(f"status must be open, closed or all, got {status!r}")
    if since:
        where.append("r.first_seen_at >= ?")
        params.append(date.fromisoformat(since).isoformat())

    sql = (
        "SELECT c.name AS company, r.title, r.department, r.location, r.workplace_type, "
        "r.compensation_text, r.date_posted, r.first_seen_at, r.last_seen_at, r.closed_at, "
        "CASE WHEN r.closed_at IS NULL THEN 'open' ELSE 'closed' END AS status, "
        "r.url, r.source, r.source_job_id "
        "FROM roles r JOIN companies c ON c.id = r.company_id"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY c.name COLLATE NOCASE, r.title COLLATE NOCASE, r.id"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]
