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
        -- A company may have several boards; a board belongs to one company.
        CREATE TABLE boards (
            id               INTEGER PRIMARY KEY,
            company_id       INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            source           TEXT NOT NULL,           -- adapter name
            ref              TEXT NOT NULL,           -- board reference (slug)
            origin           TEXT NOT NULL DEFAULT 'manual',  -- manual | link | guess
            evidence         TEXT NOT NULL DEFAULT '',        -- why resolve trusted it
            mapped_at        TEXT NOT NULL,
            last_success_at  TEXT,                    -- NULL until the baseline fetch
            last_error       TEXT,                    -- cleared by the next success
            last_error_at    TEXT,
            UNIQUE (source, ref)
        )
        """,
        "CREATE INDEX boards_company ON boards (company_id)",
        """
        CREATE TABLE roles (
            id                 INTEGER PRIMARY KEY,
            source             TEXT NOT NULL,
            source_job_id      TEXT NOT NULL,
            company_id         INTEGER NOT NULL REFERENCES companies(id),
            board_id           INTEGER REFERENCES boards(id) ON DELETE SET NULL,
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
        "CREATE INDEX roles_board_open ON roles (board_id, closed_at)",
        "CREATE INDEX roles_first_seen ON roles (first_seen_at)",
        """
        CREATE TABLE role_descriptions (
            role_id      INTEGER PRIMARY KEY REFERENCES roles(id) ON DELETE CASCADE,
            description  TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE runs (
            id               INTEGER PRIMARY KEY,
            started_at       TEXT NOT NULL,
            finished_at      TEXT,
            status           TEXT NOT NULL DEFAULT 'running',  -- running | completed | interrupted
            companies_total  INTEGER NOT NULL DEFAULT 0,
            boards_total     INTEGER NOT NULL DEFAULT 0,
            boards_ok        INTEGER NOT NULL DEFAULT 0,
            boards_failed    INTEGER NOT NULL DEFAULT 0,
            roles_open       INTEGER NOT NULL DEFAULT 0,
            roles_opened     INTEGER NOT NULL DEFAULT 0,   -- excludes baseline fetches
            roles_closed     INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        -- One row per board fetched in a run. Source and ref are copied so the
        -- history survives a board being unmapped.
        CREATE TABLE run_boards (
            run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            company_id     INTEGER NOT NULL REFERENCES companies(id),
            source         TEXT NOT NULL,
            ref            TEXT NOT NULL,
            status         TEXT NOT NULL,             -- ok | error
            error          TEXT,
            roles_fetched  INTEGER,                   -- NULL when the fetch failed
            baseline       INTEGER NOT NULL DEFAULT 0,  -- 1 = this board's first successful fetch
            duration_ms    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, source, ref)
        )
        """,
        """
        CREATE TABLE company_snapshots (
            run_id              INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            company_id          INTEGER NOT NULL REFERENCES companies(id),
            taken_at            TEXT NOT NULL,
            baseline            INTEGER NOT NULL DEFAULT 0,  -- 1 = includes a board's first fetch
            open_roles          INTEGER NOT NULL,
            opened              INTEGER NOT NULL,     -- new or reopened; baseline roles not counted
            closed              INTEGER NOT NULL,     -- closed in this run
            open_by_department  TEXT NOT NULL,        -- JSON object: department -> count
            PRIMARY KEY (run_id, company_id)
        )
        """,
        """
        -- What `companies resolve` last found for a company (one row each).
        CREATE TABLE company_resolutions (
            company_id      INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
            resolved_at     TEXT NOT NULL,
            outcome         TEXT NOT NULL,   -- mapped | needs_review | unsupported | unresolved
            job_system      TEXT,            -- e.g. workday, smartrecruiters, custom
            job_system_url  TEXT,
            careers_url     TEXT,
            evidence        TEXT NOT NULL    -- JSON: candidates and notes
        )
        """,
    ],
    [
        # A job system recorded by hand (companies set-system). Automatic
        # detection never overwrites a row with manual = 1.
        "ALTER TABLE company_resolutions ADD COLUMN manual INTEGER NOT NULL DEFAULT 0",
        # Small facts about the database itself, e.g. when coverage was last re-checked.
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    ],
    [
        """
        -- What you decided about a role. One mark per role; personal data.
        -- A saved or applied role keeps its description after it closes.
        CREATE TABLE role_marks (
            role_id    INTEGER PRIMARY KEY REFERENCES roles(id) ON DELETE CASCADE,
            status     TEXT NOT NULL CHECK (status IN ('dismissed', 'saved', 'applied')),
            note       TEXT NOT NULL DEFAULT '',
            marked_at  TEXT NOT NULL
        )
        """,
        """
        -- One ranked shortlist per local calendar day (a rebuild on the same
        -- day replaces it), kept for 30 days so lists can be compared.
        CREATE TABLE shortlists (
            id           INTEGER PRIMARY KEY,
            list_date    TEXT NOT NULL UNIQUE,     -- local date, YYYY-MM-DD
            built_at     TEXT NOT NULL,            -- UTC
            run_id       INTEGER,                  -- the scout it followed, if any
            config_hash  TEXT NOT NULL DEFAULT '',
            eligible     INTEGER NOT NULL DEFAULT 0  -- roles that qualified before the cut
        )
        """,
        """
        CREATE TABLE shortlist_entries (
            shortlist_id  INTEGER NOT NULL REFERENCES shortlists(id) ON DELETE CASCADE,
            rank          INTEGER NOT NULL,
            role_id       INTEGER NOT NULL REFERENCES roles(id),
            score         REAL NOT NULL,
            reasons       TEXT NOT NULL,           -- JSON array of plain-language reasons
            PRIMARY KEY (shortlist_id, rank)
        )
        """,
        "CREATE INDEX shortlist_entries_role ON shortlist_entries (role_id)",
    ],
    [
        """
        -- Roles that scored high enough for a shortlist but were held back
        -- because their company already had its full share of slots.
        CREATE TABLE shortlist_held (
            shortlist_id  INTEGER NOT NULL REFERENCES shortlists(id) ON DELETE CASCADE,
            role_id       INTEGER NOT NULL REFERENCES roles(id),
            score         REAL NOT NULL,
            PRIMARY KEY (shortlist_id, role_id)
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


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with transaction(conn):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


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
