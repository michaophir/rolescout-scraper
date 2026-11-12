"""The scout run: fetch every active, mapped company and record what changed."""

import json
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from rolescout.adapters import get_adapter
from rolescout.db import transaction, utc_now
from rolescout.http import make_session
from rolescout.models import Role

# A role is closed once it has been absent from this many consecutive
# successful fetches of its company. Failed fetches are not counted.
MISSES_TO_CLOSE = 2
NO_DEPARTMENT = "(none)"

ROLE_COLUMNS = ("title", "department", "location", "workplace_type", "url",
                "date_posted", "compensation_text")


@dataclass
class CompanyResult:
    company: str
    source: str
    ref: str
    status: str = "ok"           # ok | error
    error: str | None = None
    roles_fetched: int | None = None
    open_roles: int = 0
    opened: int = 0
    closed: int = 0
    open_by_department: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0


@dataclass
class RunSummary:
    run_id: int
    started_at: str
    finished_at: str = ""
    status: str = "running"
    unmapped: int = 0            # active companies skipped for lack of a mapping
    companies: list[CompanyResult] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return sum(c.status == "ok" for c in self.companies)

    @property
    def failed(self) -> int:
        return len(self.companies) - self.ok

    def total(self, attr: str) -> int:
        return sum(getattr(c, attr) for c in self.companies)


def apply_fetch(conn: sqlite3.Connection, company_id: int, roles: Iterable[Role],
                now: str) -> tuple[int, int]:
    """Record one successful fetch of a company. Returns (opened, closed).

    Must run inside a transaction. Every fetched role is inserted or updated
    and marked seen; a closed role that shows up again reopens. Open roles of
    the company that were not fetched get one more miss, and close on the
    second: the description is deleted, the one-line row is kept.
    """
    opened = 0
    seen: set[int] = set()
    for role in roles:
        values = [getattr(role, col) for col in ROLE_COLUMNS]
        row = conn.execute(
            "SELECT id, closed_at FROM roles WHERE source = ? AND source_job_id = ?",
            (role.source, role.source_job_id)).fetchone()
        if row is None:
            role_id = conn.execute(
                f"INSERT INTO roles (source, source_job_id, company_id, {', '.join(ROLE_COLUMNS)}, "
                f"first_seen_at, last_seen_at) VALUES ({', '.join('?' * (len(ROLE_COLUMNS) + 5))})",
                (role.source, role.source_job_id, company_id, *values, now, now)).lastrowid
            opened += 1
        else:
            role_id = row["id"]
            if role_id in seen:
                continue  # the source listed the same posting twice
            conn.execute(
                f"UPDATE roles SET company_id = ?, {', '.join(f'{c} = ?' for c in ROLE_COLUMNS)}, "
                "last_seen_at = ?, closed_at = NULL, missed_fetches = 0 WHERE id = ?",
                (company_id, *values, now, role_id))
            if row["closed_at"] is not None:
                opened += 1  # reopened
        seen.add(role_id)
        # The WHERE skips the write when the text is unchanged.
        conn.execute(
            "INSERT INTO role_descriptions (role_id, description) VALUES (?, ?) "
            "ON CONFLICT (role_id) DO UPDATE SET description = excluded.description "
            "WHERE description IS NOT excluded.description",
            (role_id, role.description))

    open_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM roles WHERE company_id = ? AND closed_at IS NULL", (company_id,))}
    missing = [(role_id,) for role_id in open_ids - seen]
    conn.executemany("UPDATE roles SET missed_fetches = missed_fetches + 1 WHERE id = ?", missing)
    closing = [(r["id"],) for r in conn.execute(
        "SELECT id FROM roles WHERE company_id = ? AND closed_at IS NULL AND missed_fetches >= ?",
        (company_id, MISSES_TO_CLOSE))]
    conn.executemany("DELETE FROM role_descriptions WHERE role_id = ?", closing)
    conn.executemany("UPDATE roles SET closed_at = ? WHERE id = ?", [(now, *c) for c in closing])
    return opened, len(closing)


def _open_by_department(conn, company_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT CASE WHEN department = '' THEN ? ELSE department END AS dept, COUNT(*) AS n "
        "FROM roles WHERE company_id = ? AND closed_at IS NULL GROUP BY dept ORDER BY n DESC, dept",
        (NO_DEPARTMENT, company_id))
    return {r["dept"]: r["n"] for r in rows}


def run_scout(
    conn: sqlite3.Connection,
    *,
    delay: float = 1.0,
    adapter_for: Callable[[str], object] | None = None,
    clock: Callable[[], str] = utc_now,
    on_company: Callable[[CompanyResult], None] | None = None,
) -> RunSummary:
    """Fetch every active, mapped company once and record the run.

    Each company is committed on its own, so a failure (or an interrupt)
    never undoes the companies already done. ``delay`` is the minimum number
    of seconds between HTTP requests. ``adapter_for`` and ``clock`` exist so
    tests can run without a network or a real clock.
    """
    if adapter_for is None:
        session = make_session(min_interval=delay)
        adapters: dict[str, object] = {}

        def adapter_for(source: str):
            if source not in adapters:
                adapters[source] = get_adapter(source, session)
            return adapters[source]

    now = clock()
    targets = conn.execute(
        "SELECT c.id, c.name, s.source, s.ref FROM companies c "
        "JOIN company_sources s ON s.company_id = c.id WHERE c.active = 1 "
        "ORDER BY c.tier IS NULL, c.tier, c.name").fetchall()
    unmapped = conn.execute(
        "SELECT COUNT(*) FROM companies c WHERE c.active = 1 AND NOT EXISTS "
        "(SELECT 1 FROM company_sources s WHERE s.company_id = c.id)").fetchone()[0]
    with transaction(conn):
        run_id = conn.execute(
            "INSERT INTO runs (started_at, companies_total) VALUES (?, ?)",
            (now, len(targets))).lastrowid
    summary = RunSummary(run_id=run_id, started_at=now, unmapped=unmapped)

    try:
        for target in targets:
            result = CompanyResult(company=target["name"], source=target["source"], ref=target["ref"])
            started = time.monotonic()
            roles: list[Role] = []
            try:
                roles = adapter_for(target["source"]).fetch(target["ref"], company=target["name"])
                result.roles_fetched = len(roles)
            except Exception as exc:  # one company's failure must not stop the run
                result.status = "error"
                result.error = str(exc) or type(exc).__name__
            result.duration_ms = int((time.monotonic() - started) * 1000)

            with transaction(conn):
                if result.status == "ok":
                    result.opened, result.closed = apply_fetch(conn, target["id"], roles, now)
                    conn.execute(
                        "UPDATE company_sources SET last_success_at = ?, last_error = NULL, "
                        "last_error_at = NULL WHERE company_id = ?", (now, target["id"]))
                else:
                    conn.execute(
                        "UPDATE company_sources SET last_error = ?, last_error_at = ? "
                        "WHERE company_id = ?", (result.error, now, target["id"]))
                result.open_by_department = _open_by_department(conn, target["id"])
                result.open_roles = sum(result.open_by_department.values())
                conn.execute(
                    "INSERT INTO run_companies (run_id, company_id, source, ref, status, error, "
                    "roles_fetched, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, target["id"], result.source, result.ref, result.status, result.error,
                     result.roles_fetched, result.duration_ms))
                conn.execute(
                    "INSERT INTO company_snapshots (run_id, company_id, taken_at, open_roles, "
                    "opened, closed, open_by_department) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, target["id"], now, result.open_roles, result.opened, result.closed,
                     json.dumps(result.open_by_department, ensure_ascii=False)))
            summary.companies.append(result)
            if on_company:
                on_company(result)
        summary.status = "completed"
    except KeyboardInterrupt:
        summary.status = "interrupted"
        raise
    finally:
        if summary.status == "running":
            summary.status = "interrupted"
        summary.finished_at = utc_now() if clock is utc_now else clock()
        with transaction(conn):
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, companies_ok = ?, companies_failed = ?, "
                "roles_open = ?, roles_opened = ?, roles_closed = ? WHERE id = ?",
                (summary.finished_at, summary.status, summary.ok, summary.failed,
                 summary.total("open_roles"), summary.total("opened"), summary.total("closed"),
                 run_id))
    return summary
