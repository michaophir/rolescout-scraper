"""The scout run: fetch every board of every active company and record what changed."""

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import groupby

from rolescout.adapters import FetchContext, KnownRole, get_adapter
from rolescout.db import transaction, utc_now
from rolescout.http import make_session
from rolescout.models import Role

try:
    import fcntl
except ImportError:  # not a Unix system: no lock is taken
    fcntl = None

# A role is closed once it has been absent from this many consecutive
# successful fetches of its board. Failed fetches are not counted.
MISSES_TO_CLOSE = 2
NO_DEPARTMENT = "(none)"

ROLE_COLUMNS = ("title", "department", "location", "workplace_type", "url",
                "date_posted", "compensation_text")


@dataclass
class BoardResult:
    company: str
    source: str
    ref: str
    status: str = "ok"           # ok | error
    error: str | None = None
    roles_fetched: int | None = None
    baseline: bool = False       # this board's first successful fetch
    duration_ms: int = 0
    requests: int | None = None  # HTTP requests the fetch made, when the adapter counts them
    complete: bool = True        # False: the fetch stopped early, so nothing was closed
    note: str = ""


@dataclass
class CompanySnapshot:
    company: str
    baseline: bool = False       # includes at least one board's first fetch
    open_roles: int = 0
    opened: int = 0              # new or reopened; baseline roles are not counted
    closed: int = 0
    open_by_department: dict[str, int] = field(default_factory=dict)


@dataclass
class RunSummary:
    run_id: int
    started_at: str
    finished_at: str = ""
    status: str = "running"
    unmapped: int = 0            # active companies skipped for lack of a board
    boards: list[BoardResult] = field(default_factory=list)
    snapshots: list[CompanySnapshot] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return sum(b.status == "ok" for b in self.boards)

    @property
    def failed(self) -> int:
        return len(self.boards) - self.ok

    def total(self, attr: str) -> int:
        return sum(getattr(s, attr) for s in self.snapshots)


class ScoutAlreadyRunning(Exception):
    """Another scout holds the lock on this database."""


def database_file(conn: sqlite3.Connection) -> str:
    """Path of the connection's main database file ("" for an in-memory one)."""
    return conn.execute("PRAGMA database_list").fetchone()[2] or ""


@contextmanager
def scout_lock(conn: sqlite3.Connection):
    """Hold the one-scout-at-a-time lock for this database.

    An OS file lock on ``<database>.lock``: it disappears with the process,
    so a crashed scout never leaves a stale lock behind.
    """
    path = database_file(conn)
    if not path or fcntl is None:
        yield
        return
    handle = open(path + ".lock", "a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            pid = handle.read().strip()
            raise ScoutAlreadyRunning(
                "another scout is already running on this database"
                + (f" (pid {pid})" if pid else "")) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
        handle.seek(0)
        handle.truncate()
    finally:
        handle.close()  # closing releases the lock


def scout_is_running(conn: sqlite3.Connection) -> bool:
    try:
        with scout_lock(conn):
            return False
    except ScoutAlreadyRunning:
        return True


def apply_fetch(conn: sqlite3.Connection, company_id: int, board_id: int,
                roles: Iterable[Role], now: str, *, complete: bool = True) -> tuple[int, int, int]:
    """Record one successful fetch of a board. Returns (new, reopened, closed).

    Must run inside a transaction. Every fetched role is inserted or updated
    and marked seen; a closed role that shows up again reopens. Open roles of
    the board that were not fetched get one more miss, and close on the
    second: the description is deleted (unless the role is marked saved or
    applied), the one-line row is kept.

    A role fetched without a description keeps the one already stored: some
    sources charge a request per description and are asked only once.
    With ``complete`` False the fetch did not see the whole board, so no role
    is counted as missing.
    """
    new = reopened = 0
    seen: set[int] = set()
    for role in roles:
        values = [getattr(role, col) for col in ROLE_COLUMNS]
        row = conn.execute(
            "SELECT id, closed_at FROM roles WHERE source = ? AND source_job_id = ?",
            (role.source, role.source_job_id)).fetchone()
        if row is None:
            role_id = conn.execute(
                f"INSERT INTO roles (source, source_job_id, company_id, board_id, "
                f"{', '.join(ROLE_COLUMNS)}, first_seen_at, last_seen_at) "
                f"VALUES ({', '.join('?' * (len(ROLE_COLUMNS) + 6))})",
                (role.source, role.source_job_id, company_id, board_id, *values, now, now)).lastrowid
            new += 1
        else:
            role_id = row["id"]
            if role_id in seen:
                continue  # the source listed the same posting twice
            conn.execute(
                f"UPDATE roles SET company_id = ?, board_id = ?, "
                f"{', '.join(f'{c} = ?' for c in ROLE_COLUMNS)}, "
                "last_seen_at = ?, closed_at = NULL, missed_fetches = 0 WHERE id = ?",
                (company_id, board_id, *values, now, role_id))
            if row["closed_at"] is not None:
                reopened += 1
        seen.add(role_id)
        if role.description:
            # The WHERE skips the write when the text is unchanged.
            conn.execute(
                "INSERT INTO role_descriptions (role_id, description) VALUES (?, ?) "
                "ON CONFLICT (role_id) DO UPDATE SET description = excluded.description "
                "WHERE description IS NOT excluded.description",
                (role_id, role.description))

    if not complete:
        return new, reopened, 0
    open_ids = {r["id"] for r in conn.execute(
        "SELECT id FROM roles WHERE board_id = ? AND closed_at IS NULL", (board_id,))}
    missing = [(role_id,) for role_id in open_ids - seen]
    conn.executemany("UPDATE roles SET missed_fetches = missed_fetches + 1 WHERE id = ?", missing)
    closing = [(r["id"],) for r in conn.execute(
        "SELECT id FROM roles WHERE board_id = ? AND closed_at IS NULL AND missed_fetches >= ?",
        (board_id, MISSES_TO_CLOSE))]
    # A role you saved or applied to keeps its description after it closes.
    conn.executemany(
        "DELETE FROM role_descriptions WHERE role_id = ? AND role_id NOT IN "
        "(SELECT role_id FROM role_marks WHERE status IN ('saved', 'applied'))", closing)
    conn.executemany("UPDATE roles SET closed_at = ? WHERE id = ?", [(now, *c) for c in closing])
    return new, reopened, len(closing)


def known_roles(conn: sqlite3.Connection, board_id: int) -> dict[str, KnownRole]:
    """What is stored for the roles of one board, for adapters that fetch
    descriptions one at a time and must not fetch one twice."""
    rows = conn.execute(
        "SELECT r.source_job_id, r.date_posted, r.location, r.workplace_type, "
        "EXISTS (SELECT 1 FROM role_descriptions d WHERE d.role_id = r.id AND d.description != '') "
        "  AS has_description, "
        "EXISTS (SELECT 1 FROM role_marks m WHERE m.role_id = r.id "
        "        AND m.status IN ('saved', 'applied')) AS keep "
        "FROM roles r WHERE r.board_id = ?", (board_id,))
    return {r["source_job_id"]: KnownRole(
        date_posted=r["date_posted"], location=r["location"], workplace_type=r["workplace_type"],
        has_description=bool(r["has_description"]), keep=bool(r["keep"])) for r in rows}


def _open_by_department(conn, company_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT CASE WHEN department = '' THEN ? ELSE department END AS dept, COUNT(*) AS n "
        "FROM roles WHERE company_id = ? AND closed_at IS NULL GROUP BY dept ORDER BY n DESC, dept",
        (NO_DEPARTMENT, company_id))
    return {r["dept"]: r["n"] for r in rows}


def run_scout(conn: sqlite3.Connection, **kwargs) -> RunSummary:
    """Run one scout, or raise ``ScoutAlreadyRunning`` if one is in progress.

    See ``_run_scout`` for the arguments.
    """
    with scout_lock(conn):
        return _run_scout(conn, **kwargs)


def _run_scout(
    conn: sqlite3.Connection,
    *,
    delay: float = 1.0,
    adapter_for: Callable[[str], object] | None = None,
    clock: Callable[[], str] = utc_now,
    on_board: Callable[[BoardResult], None] | None = None,
    wants_description: Callable[[str], bool] | None = None,
) -> RunSummary:
    """Fetch every board of every active company once and record the run.

    ``wants_description`` says, from a role's title, whether its description
    is worth a request of its own. It matters only for sources that charge
    one request per description (Workday); without it those sources fetch
    descriptions for saved and applied roles only.

    Each company is committed on its own, so a failure (or an interrupt)
    never undoes the companies already done. ``delay`` is the minimum number
    of seconds between HTTP requests. ``adapter_for`` and ``clock`` exist so
    tests can run without a network or a real clock.

    A board's first successful fetch is a baseline: its roles are stored with
    first-seen dates but not counted as opened.
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
        "SELECT c.id AS company_id, c.name, b.id AS board_id, b.source, b.ref, b.last_success_at "
        "FROM companies c JOIN boards b ON b.company_id = c.id WHERE c.active = 1 "
        "ORDER BY c.tier IS NULL, c.tier, c.name, b.id").fetchall()
    unmapped = conn.execute(
        "SELECT COUNT(*) FROM companies c WHERE c.active = 1 AND NOT EXISTS "
        "(SELECT 1 FROM boards b WHERE b.company_id = c.id)").fetchone()[0]
    with transaction(conn):
        run_id = conn.execute(
            "INSERT INTO runs (started_at, companies_total, boards_total) VALUES (?, ?, ?)",
            (now, len({t["company_id"] for t in targets}), len(targets))).lastrowid
    summary = RunSummary(run_id=run_id, started_at=now, unmapped=unmapped)

    try:
        for company_id, boards in groupby(targets, key=lambda t: t["company_id"]):
            boards = list(boards)
            name = boards[0]["name"]
            fetched: list[tuple[sqlite3.Row, BoardResult, list[Role]]] = []
            for board in boards:
                result = BoardResult(company=name, source=board["source"], ref=board["ref"])
                started = time.monotonic()
                roles: list[Role] = []
                adapter = None
                try:
                    adapter = adapter_for(board["source"])
                    if getattr(adapter, "uses_context", False):
                        context = FetchContext(
                            wants_description=wants_description or (lambda title: False),
                            known=known_roles(conn, board["board_id"]))
                        roles = adapter.fetch(board["ref"], company=name, context=context)
                    else:
                        roles = adapter.fetch(board["ref"], company=name)
                    result.roles_fetched = len(roles)
                    result.baseline = board["last_success_at"] is None
                except Exception as exc:  # one board's failure must not stop the run
                    result.status = "error"
                    result.error = str(exc) or type(exc).__name__
                stats = getattr(adapter, "last_stats", None)
                if stats is not None:
                    result.requests = stats.requests
                    result.complete = stats.complete or result.status != "ok"
                    result.note = "; ".join(stats.notes) if result.status == "ok" else ""
                result.duration_ms = int((time.monotonic() - started) * 1000)
                fetched.append((board, result, roles))

            snapshot = CompanySnapshot(company=name)
            with transaction(conn):
                for board, result, roles in fetched:
                    if result.status == "ok":
                        new, reopened, closed = apply_fetch(
                            conn, company_id, board["board_id"], roles, now,
                            complete=result.complete)
                        snapshot.opened += reopened + (0 if result.baseline else new)
                        snapshot.closed += closed
                        snapshot.baseline = snapshot.baseline or result.baseline
                        # A first fetch that was cut short is not the baseline
                        # yet: the roles it did not reach must not count as
                        # newly opened when a later fetch finds them.
                        conn.execute(
                            "UPDATE boards SET last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END, "
                            "last_error = NULL, last_error_at = NULL WHERE id = ?",
                            (int(result.complete or not result.baseline), now, board["board_id"]))
                    else:
                        conn.execute(
                            "UPDATE boards SET last_error = ?, last_error_at = ? WHERE id = ?",
                            (result.error, now, board["board_id"]))
                    conn.execute(
                        "INSERT INTO run_boards (run_id, company_id, source, ref, status, error, "
                        "roles_fetched, baseline, duration_ms, requests, note) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_id, company_id, result.source, result.ref, result.status,
                         result.error, result.roles_fetched, int(result.baseline),
                         result.duration_ms, result.requests, result.note))
                snapshot.open_by_department = _open_by_department(conn, company_id)
                snapshot.open_roles = sum(snapshot.open_by_department.values())
                conn.execute(
                    "INSERT INTO company_snapshots (run_id, company_id, taken_at, baseline, "
                    "open_roles, opened, closed, open_by_department) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, company_id, now, int(snapshot.baseline), snapshot.open_roles,
                     snapshot.opened, snapshot.closed,
                     json.dumps(snapshot.open_by_department, ensure_ascii=False)))
            summary.snapshots.append(snapshot)
            for _, result, _ in fetched:
                summary.boards.append(result)
                if on_board:
                    on_board(result)
        summary.status = "completed"
    finally:
        if summary.status == "running":
            summary.status = "interrupted"
        summary.finished_at = utc_now() if clock is utc_now else clock()
        with transaction(conn):
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = ?, boards_ok = ?, boards_failed = ?, "
                "roles_open = ?, roles_opened = ?, roles_closed = ? WHERE id = ?",
                (summary.finished_at, summary.status, summary.ok, summary.failed,
                 summary.total("open_roles"), summary.total("opened"), summary.total("closed"),
                 run_id))
    return summary
