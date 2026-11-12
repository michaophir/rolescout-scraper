"""SQLite storage: location, connection settings and schema migrations."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_ENV_VAR = "ROLESCOUT_DB"
DEFAULT_DB_PATH = Path("~/.rolescout/rolescout.db")

# One entry per schema version; entry N upgrades a database from version N to
# N+1. Never edit a released entry: append a new one. The current version is
# stored in SQLite's `PRAGMA user_version`.
MIGRATIONS: list[list[str]] = [
    [
        """
        CREATE TABLE companies (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            website     TEXT NOT NULL DEFAULT '',
            tier        INTEGER,
            tags        TEXT NOT NULL DEFAULT '[]',   -- JSON array of strings
            active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at  TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE company_sources (
            company_id       INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
            source           TEXT NOT NULL,           -- adapter name
            ref              TEXT NOT NULL,           -- board reference (slug)
            mapped_at        TEXT NOT NULL,
            last_success_at  TEXT,
            last_error       TEXT,                    -- cleared by the next success
            last_error_at    TEXT,
            UNIQUE (source, ref)
        )
        """,
        """
        CREATE TABLE roles (
            id                 INTEGER PRIMARY KEY,
            source             TEXT NOT NULL,
            source_job_id      TEXT NOT NULL,
            company_id         INTEGER NOT NULL REFERENCES companies(id),
            title              TEXT NOT NULL DEFAULT '',
            department         TEXT NOT NULL DEFAULT '',
            location           TEXT NOT NULL DEFAULT '',
            workplace_type     TEXT NOT NULL DEFAULT '',
            url                TEXT NOT NULL DEFAULT '',
            date_posted        TEXT NOT NULL DEFAULT '',
            compensation_text  TEXT NOT NULL DEFAULT '',
            first_seen_at      TEXT NOT NULL,
            last_seen_at       TEXT NOT NULL,
            closed_at          TEXT,                  -- NULL while open
            missed_fetches     INTEGER NOT NULL DEFAULT 0,  -- consecutive successful fetches without it
            UNIQUE (source, source_job_id)
        )
        """,
        "CREATE INDEX roles_company_open ON roles (company_id, closed_at)",
        "CREATE INDEX roles_first_seen ON roles (first_seen_at)",
        """
        CREATE TABLE role_descriptions (
            role_id      INTEGER PRIMARY KEY REFERENCES roles(id) ON DELETE CASCADE,
            description  TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE runs (
            id                INTEGER PRIMARY KEY,
            started_at        TEXT NOT NULL,
            finished_at       TEXT,
            status            TEXT NOT NULL DEFAULT 'running',  -- running | completed | interrupted
            companies_total   INTEGER NOT NULL DEFAULT 0,
            companies_ok      INTEGER NOT NULL DEFAULT 0,
            companies_failed  INTEGER NOT NULL DEFAULT 0,
            roles_open        INTEGER NOT NULL DEFAULT 0,
            roles_opened      INTEGER NOT NULL DEFAULT 0,
            roles_closed      INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE run_companies (
            run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            company_id     INTEGER NOT NULL REFERENCES companies(id),
            source         TEXT NOT NULL,
            ref            TEXT NOT NULL,
            status         TEXT NOT NULL,             -- ok | error
            error          TEXT,
            roles_fetched  INTEGER,                   -- NULL when the fetch failed
            duration_ms    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, company_id)
        )
        """,
        """
        CREATE TABLE company_snapshots (
            run_id              INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            company_id          INTEGER NOT NULL REFERENCES companies(id),
            taken_at            TEXT NOT NULL,
            open_roles          INTEGER NOT NULL,
            opened              INTEGER NOT NULL,     -- new or reopened in this run
            closed              INTEGER NOT NULL,     -- closed in this run
            open_by_department  TEXT NOT NULL,        -- JSON object: department -> count
            PRIMARY KEY (run_id, company_id)
        )
        """,
    ],
]

SCHEMA_VERSION = len(MIGRATIONS)


class SchemaError(Exception):
    """The database was written by a newer rolescout than this one."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_db_path(flag: str | os.PathLike | None = None) -> Path:
    """--db flag, then $ROLESCOUT_DB, then ~/.rolescout/rolescout.db."""
    chosen = flag or os.environ.get(DB_ENV_VAR) or DEFAULT_DB_PATH
    return Path(chosen).expanduser()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """All-or-nothing block. Takes the write lock up front."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"database schema is version {current}, but this rolescout only knows "
            f"up to version {SCHEMA_VERSION}; upgrade rolescout")
    for version in range(current, SCHEMA_VERSION):
        with transaction(conn):
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version + 1}")


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """Open (creating and migrating if needed) the database at ``path``."""
    db_path = resolve_db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: no implicit transactions; writes use transaction().
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn
