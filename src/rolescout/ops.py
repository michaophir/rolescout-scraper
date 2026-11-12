"""Running unattended: backups, log rotation and the status report."""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rolescout.scout import database_file, scout_is_running

BACKUP_PREFIX = "rolescout-"
BACKUP_SUFFIX = ".db"
FAILING_AFTER_RUNS = 3          # "more than two runs in a row"
STALE_AFTER_HOURS = 26          # a daily schedule that has not run for this long missed a day
LOG_MAX_BYTES = 1_000_000
LOG_KEEP = 3


class BackupError(Exception):
    pass


# --- backup ------------------------------------------------------------------

def list_backups(directory: str | os.PathLike) -> list[Path]:
    """Backups in a directory, oldest first (the names sort by time)."""
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if p.is_file())


def backup_database(conn: sqlite3.Connection, directory: str | os.PathLike, *, keep: int = 7,
                    now: datetime | None = None) -> tuple[Path, list[Path]]:
    """Write a consistent copy of the database and prune old copies.

    Uses SQLite's online backup, which reads one committed snapshot, so it is
    safe while a scout is writing. The copy is checked before it replaces
    anything: it is written under a temporary name and renamed when good.
    Returns (new backup, backups deleted).
    """
    if keep < 1:
        raise BackupError("--keep must be at least 1")
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    target = directory / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    partial = directory / f".{target.name}.partial"
    partial.unlink(missing_ok=True)
    try:
        copy = sqlite3.connect(partial)
        try:
            conn.backup(copy)
            # A single self-contained file: no -wal/-shm companions to lose.
            copy.execute("PRAGMA journal_mode = DELETE")
            check = copy.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            copy.close()
        if check != "ok":
            raise BackupError(f"backup failed its integrity check: {check}")
        os.replace(partial, target)
    except sqlite3.Error as exc:
        raise BackupError(f"backup failed: {exc}") from exc
    finally:
        partial.unlink(missing_ok=True)

    backups = list_backups(directory)
    removed = backups[:-keep]
    for old in removed:
        old.unlink()
    return target, removed


# --- log rotation ------------------------------------------------------------

def rotate_log(path: str | os.PathLike, *, max_bytes: int = LOG_MAX_BYTES, keep: int = LOG_KEEP) -> bool:
    """Once the log passes ``max_bytes``: log -> log.1 -> log.2 ..., keeping ``keep`` old files."""
    path = Path(path)
    if not path.exists() or path.stat().st_size < max_bytes:
        return False
    Path(f"{path}.{keep}").unlink(missing_ok=True)
    for n in range(keep - 1, 0, -1):
        older = Path(f"{path}.{n}")
        if older.exists():
            older.rename(f"{path}.{n + 1}")
    path.rename(f"{path}.1")
    return True


# --- status ------------------------------------------------------------------

def _parse_utc(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def collect_status(conn: sqlite3.Connection, *, schedule: dict | None = None,
                   now: datetime | None = None) -> dict:
    """Everything ``rolescout status`` shows. Reads the database and local files only.

    ``schedule`` is what ``rolescout.schedule.read_schedule()`` returned.
    """
    now = now or datetime.now(timezone.utc)
    db_path = database_file(conn)
    size = sum(Path(db_path + suffix).stat().st_size
               for suffix in ("", "-wal") if db_path and Path(db_path + suffix).exists())

    status: dict = {
        "database": db_path, "database_bytes": size,
        "scout_running": scout_is_running(conn),
        "open_roles": conn.execute(
            "SELECT COUNT(*) FROM roles r JOIN companies c ON c.id = r.company_id "
            "WHERE r.closed_at IS NULL AND c.active = 1").fetchone()[0],
        "companies_active": conn.execute("SELECT COUNT(*) FROM companies WHERE active = 1").fetchone()[0],
        "companies_mapped": conn.execute(
            "SELECT COUNT(DISTINCT b.company_id) FROM boards b JOIN companies c ON c.id = b.company_id "
            "WHERE c.active = 1").fetchone()[0],
        "last_run": None, "failing_boards": [], "schedule": schedule, "last_backup": None,
        "attention": [],
    }

    run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if run is not None:
        per_company = conn.execute(
            "SELECT company_id, SUM(status != 'ok') AS failed FROM run_boards WHERE run_id = ? "
            "GROUP BY company_id", (run["id"],)).fetchall()
        failed_now = conn.execute(
            "SELECT c.name, rb.source, rb.ref, rb.error FROM run_boards rb "
            "JOIN companies c ON c.id = rb.company_id WHERE rb.run_id = ? AND rb.status != 'ok' "
            "ORDER BY c.name", (run["id"],)).fetchall()
        started = _parse_utc(run["started_at"])
        status["last_run"] = {
            "id": run["id"], "started_at": run["started_at"], "finished_at": run["finished_at"],
            "status": run["status"], "hours_ago": round((now - started).total_seconds() / 3600, 1),
            "boards_ok": run["boards_ok"], "boards_failed": run["boards_failed"],
            "companies_ok": sum(1 for c in per_company if not c["failed"]),
            "companies_failed": sum(1 for c in per_company if c["failed"]),
            "roles_open": run["roles_open"], "roles_opened": run["roles_opened"],
            "roles_closed": run["roles_closed"],
            "failed": [dict(f) for f in failed_now],
        }

    # Boards whose most recent fetches all failed, counted back from the latest.
    for board in conn.execute(
            "SELECT b.source, b.ref, b.last_success_at, b.last_error, c.name AS company "
            "FROM boards b JOIN companies c ON c.id = b.company_id WHERE c.active = 1 "
            "AND b.last_error IS NOT NULL ORDER BY c.name"):
        streak = 0
        for attempt in conn.execute(
                "SELECT status FROM run_boards WHERE source = ? AND ref = ? ORDER BY run_id DESC",
                (board["source"], board["ref"])):
            if attempt["status"] == "ok":
                break
            streak += 1
        if streak >= FAILING_AFTER_RUNS:
            status["failing_boards"].append({**dict(board), "failed_runs_in_a_row": streak})

    if schedule and schedule.get("backup_dir"):
        backups = list_backups(schedule["backup_dir"])
        if backups:
            newest = backups[-1]
            status["last_backup"] = {
                "path": str(newest), "bytes": newest.stat().st_size, "count": len(backups),
                "modified": datetime.fromtimestamp(newest.stat().st_mtime, timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")}

    # What deserves a look.
    attention = status["attention"]
    last = status["last_run"]
    scheduled = bool(schedule and schedule.get("loaded"))
    if last is None:
        attention.append("no scout has run yet")
    else:
        if last["status"] == "interrupted":
            attention.append("the last run was interrupted before it finished")
        elif last["status"] == "running" and not status["scout_running"]:
            attention.append("the last run never finished (the process died)")
        elif last["status"] == "completed" and last["boards_ok"] == 0 and last["boards_failed"]:
            attention.append("the last run could not fetch any board (network down?)")
        if scheduled and now - started > timedelta(hours=STALE_AFTER_HOURS):
            attention.append(f"no run for {last['hours_ago']:.0f} hours although a daily run is scheduled")
    for board in status["failing_boards"]:
        attention.append(
            f"{board['company']} ({board['source']}/{board['ref']}) has failed "
            f"{board['failed_runs_in_a_row']} runs in a row: {board['last_error']}")
    if schedule and schedule.get("installed") and not schedule.get("loaded"):
        attention.append("the schedule file exists but launchd has not loaded it; "
                         "run: rolescout schedule install")
    status["healthy"] = not attention
    return status
