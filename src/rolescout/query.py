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
    text: list[str] | None = None,
    status: str = "open",          # open | closed | all
    since: str | None = None,      # YYYY-MM-DD: first seen on or after this date
    include_inactive: bool = False,
    limit: int | None = None,
    offset: int = 0,
    only=None,                     # a rolescout.filters.RoleFilter
) -> list[dict]:
    """Roles matching every given filter, ordered by company then title.

    ``title`` is a list of phrases; a role matches if its title contains any
    one of them (case-insensitive). ``text`` is the same for the description:
    a role matches if its stored description contains any one of the phrases,
    and comes back with ``excerpt``, the text around the first one found. A
    role with no stored description never matches (a closed role, unless it
    is saved or applied, and most roles of the sources that charge a request
    per description).

    Open roles of deactivated companies are left out unless
    ``include_inactive`` is set: nothing refreshes them, so "open" is stale.
    Their closed roles are history and always included.

    ``only`` narrows the result to companies with the wanted stage, sectors,
    HQ, size or VC, and to roles in the wanted place.
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
    wanted = [p.strip() for p in text or [] if p.strip()]
    if wanted:
        where.append("(" + " OR ".join(["d.description LIKE ? ESCAPE '\\'"] * len(wanted)) + ")")
        params.extend(_like(p) for p in wanted)
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
        "SELECT r.id, c.name AS company, r.title, r.department, r.location, r.workplace_type, "
        "r.compensation_text, r.pay_min, r.pay_max, r.pay_currency, r.pay_period, r.pay_varies, r.pay_source, "
        "r.date_posted, r.first_seen_at, r.last_seen_at, r.closed_at, "
        "CASE WHEN r.closed_at IS NULL THEN 'open' ELSE 'closed' END AS status, "
        "r.url, r.source, r.source_job_id"
        + (", d.description AS excerpt " if wanted else " ")
        + "FROM roles r JOIN companies c ON c.id = r.company_id"
        + (" JOIN role_descriptions d ON d.role_id = r.id" if wanted else "")
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY c.name COLLATE NOCASE, r.title COLLATE NOCASE, r.id"
    )
    if only:
        # The place filter reads each role's location text, so it is applied
        # here and the page is cut afterwards.
        companies = only.company_ids(conn)
        rows = [dict(row) for row in conn.execute(sql.replace("SELECT r.id,", "SELECT r.company_id, r.id,", 1), params)]
        rows = [r for r in rows if (companies is None or r.pop("company_id") in companies)
                and only.role_matches(r["location"], r["workplace_type"])]
        for r in rows:
            r.pop("company_id", None)
        start = max(0, offset)
        return _excerpts(rows[start:] if limit is None else rows[start:start + limit], wanted)
    if limit is not None or offset:
        sql += " LIMIT ? OFFSET ?"
        params.extend([-1 if limit is None else limit, max(0, offset)])
    return _excerpts([dict(row) for row in conn.execute(sql, params)], wanted)


EXCERPT_BEFORE, EXCERPT_AFTER = 60, 100


def _excerpts(rows: list[dict], phrases: list[str]) -> list[dict]:
    """Replace each row's description with the text around the first phrase found in it."""
    for row in rows if phrases else []:
        description = " ".join((row["excerpt"] or "").split())
        lowered = description.lower()
        at = min((i for i in (lowered.find(p.lower()) for p in phrases) if i >= 0), default=0)
        start, end = max(0, at - EXCERPT_BEFORE), min(len(description), at + EXCERPT_AFTER)
        row["excerpt"] = ("…" if start else "") + description[start:end] + ("…" if end < len(description) else "")
    return rows
