"""What rolescout is doing right now.

Every long job (scout, resolve, harvest, and the daily job that runs them)
keeps one row in the ``jobs`` table while it works: when it started, how far
it is, and the process doing it. ``rolescout status`` and the MCP server read
that row from another process, so nobody has to look at the machine's
process list.

A job that ends normally removes its row. One that stops on an error leaves
it, with the error; one whose process disappears leaves it with no ending at
all. Both are reported until the next job of the same kind starts.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from rolescout.db import transaction, utc_now

KINDS = ("daily", "scout", "resolve", "harvest")
LABELS = {"daily": "daily job", "scout": "scout", "resolve": "resolve", "harvest": "harvest"}


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True      # it exists; it is just not ours to signal
    return True


class Job:
    """A handle on this process's row. Every call is one small write."""

    def __init__(self, conn: sqlite3.Connection, job_id: int) -> None:
        self.conn = conn
        self.id = job_id

    def advance(self, done: int | None = None, *, total: int | None = None, step: str | None = None) -> None:
        """Record progress: how many units are done, of how many, and (for a
        job made of steps) what it is doing now."""
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE jobs SET done = COALESCE(?, done), total = COALESCE(?, total), "
                    "step = COALESCE(?, step), updated_at = ? WHERE id = ?",
                    (done, total, step, utc_now(), self.id))
        except sqlite3.Error:
            pass     # progress is information; it must never stop the job itself


@contextmanager
def track(conn: sqlite3.Connection, kind: str, *, total: int | None = None, unit: str = ""):
    """Show a job as in progress for as long as the block runs."""
    now = utc_now()
    try:
        with transaction(conn):
            # A new job of a kind replaces what is left of earlier ones that are over.
            for row in conn.execute("SELECT id, pid, ended_at FROM jobs WHERE kind = ?", (kind,)).fetchall():
                if row["ended_at"] or not process_alive(row["pid"]):
                    conn.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))
            job_id = conn.execute(
                "INSERT INTO jobs (kind, pid, started_at, updated_at, total, unit) VALUES (?, ?, ?, ?, ?, ?)",
                (kind, os.getpid(), now, now, total, unit)).lastrowid
    except sqlite3.Error:
        yield Job(conn, -1)      # tracking is information; the job runs without it
        return
    job = Job(conn, job_id)
    try:
        yield job
    except KeyboardInterrupt:
        _end(conn, job_id, None)         # stopped on purpose: nothing to report
        raise
    except BaseException as exc:
        _end(conn, job_id, f"{type(exc).__name__}: {exc}"[:300])
        raise
    else:
        _end(conn, job_id, None)


def _end(conn: sqlite3.Connection, job_id: int, error: str | None) -> None:
    try:
        with transaction(conn):
            if error is None:
                conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            else:
                conn.execute("UPDATE jobs SET ended_at = ?, error = ? WHERE id = ?", (utc_now(), error, job_id))
    except sqlite3.Error:
        pass


def _local(stamp: str, now: datetime) -> str:
    """"07:30" for today, "Mon 16 Nov 07:30" for another day, in local time."""
    when = _parse(stamp).astimezone()
    same_day = when.date() == now.astimezone().date()
    return f"{when:%H:%M}" if same_day else f"{when:%a} {when.day} {when:%b} {when:%H:%M}"


def current(conn: sqlite3.Connection, now: datetime | None = None, alive=process_alive) -> dict:
    """What is running and what stopped without finishing.

    Returns ``{"running": [...], "stopped": [...], "line": "..."}``. Each job
    has kind, started_at, done, total, unit, step and, when its pace allows
    one, ``eta`` (UTC). ``line`` is the one-line summary ``status`` shows.
    """
    now = now or datetime.now(timezone.utc)
    running, stopped = [], []
    for row in conn.execute("SELECT * FROM jobs ORDER BY id"):
        job = {k: row[k] for k in ("kind", "pid", "started_at", "updated_at", "done", "total", "unit",
                                   "step", "ended_at", "error")}
        job["eta"] = None
        if row["ended_at"] or not alive(row["pid"]):
            job["died"] = not row["ended_at"]
            stopped.append(job)
            continue
        if row["total"] and row["done"] and row["done"] < row["total"]:
            spent = (_parse(row["updated_at"]) - _parse(row["started_at"])).total_seconds()
            left = spent * (row["total"] - row["done"]) / row["done"]
            job["eta"] = (_parse(row["updated_at"]) + timedelta(seconds=left)).strftime("%Y-%m-%dT%H:%M:%SZ")
        running.append(job)
    return {"running": running, "stopped": stopped, "line": _line(running, stopped, now)}


def _progress(job: dict) -> str:
    if not job["total"]:
        return f"{job['done']} {job['unit']} so far" if job["done"] and job["unit"] else ""
    return f"{job['done']} of {job['total']} {job['unit']}".rstrip()


def _describe(job: dict, now: datetime) -> str:
    parts = [f"{LABELS.get(job['kind'], job['kind'])}, started {_local(job['started_at'], now)}"]
    if _progress(job):
        parts.append(_progress(job))
    if job["step"]:
        parts.append(job["step"])
    if job["eta"]:
        parts.append(f"should finish about {_local(job['eta'], now)}")
    elif job["total"] and not job["done"]:
        parts.append("no estimate yet")
    return ", ".join(parts)


def _line(running: list[dict], stopped: list[dict], now: datetime) -> str:
    said = []
    # The daily job runs the others inside its own process: say them as its steps.
    daily = next((j for j in running if j["kind"] == "daily"), None)
    inner = [j for j in running if daily and j is not daily and j["pid"] == daily["pid"]]
    if daily:
        text = f"daily job, started {_local(daily['started_at'], now)}"
        if inner:
            text += "; now: " + "; ".join(_describe(j, now) for j in inner)
        elif daily["step"]:
            text += f"; now: {daily['step']}"
        said.append(text)
    said += [_describe(j, now) for j in running if j is not daily and j not in inner]
    line = ". Also: ".join(said) if said else "Idle"
    for job in stopped:
        where = f" at {_progress(job)}" if _progress(job) else ""
        if job["died"]:
            line += (f". A {LABELS.get(job['kind'], job['kind'])} that started {_local(job['started_at'], now)} "
                     f"died without finishing ({('stopped' + where) if where else 'no progress recorded'})")
        else:
            line += (f". A {LABELS.get(job['kind'], job['kind'])} that started {_local(job['started_at'], now)} "
                     f"stopped on an error{where}: {job['error']}")
    return line
