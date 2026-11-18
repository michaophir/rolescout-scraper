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
def _repair_websites(conn: sqlite3.Connection) -> None:
    """Give websites stored without a scheme ("alation.com") the normal form.
    A value that cannot be a website is left as it is; resolve reports it."""
    from rolescout.urls import InvalidWebsite, normalize_website

    for row in conn.execute("SELECT id, website FROM companies WHERE website != ''").fetchall():
        try:
            fixed = normalize_website(row["website"])
        except InvalidWebsite:
            continue
        if fixed != row["website"]:
            conn.execute("UPDATE companies SET website = ? WHERE id = ?", (fixed, row["id"]))


def _drop_false_workday_findings(conn: sqlite3.Connection) -> None:
    """Forget automatic "this company is on Workday" findings whose URL is not
    on a Workday host (a page that merely mentioned the domain). The company
    goes back to not resolved and is looked at again by the next resolve."""
    from rolescout.adapters.workday import is_workday_url

    for row in conn.execute(
            "SELECT company_id, job_system_url FROM company_resolutions "
            "WHERE job_system = 'workday' AND manual = 0").fetchall():
        if not is_workday_url(row["job_system_url"] or ""):
            conn.execute("DELETE FROM company_resolutions WHERE company_id = ?", (row["company_id"],))


# Each step is an SQL statement or a function taking the connection.
def _vcs_and_profiles_from_what_is_stored(conn: sqlite3.Connection) -> None:
    """Make a VC from every registered portfolio board, then copy profile data
    and VC links from linked candidates into the companies they stand for.
    Reads only the database."""
    from rolescout import profiles, vcs
    for board in conn.execute("SELECT id FROM portfolio_boards ORDER BY id").fetchall():
        vcs.ensure_for_board(conn, board["id"])
    profiles.backfill(conn)


# "Backed by" as one text value: the short names of a company's VCs, ecosystem groups left out.
_BACKED_BY = ("(SELECT group_concat(short_name, ', ') FROM (SELECT v.short_name FROM company_vcs l "
              "JOIN vcs v ON v.id = l.vc_id WHERE l.company_id = c.id AND v.type = 'vc' ORDER BY v.id))")
_ECOSYSTEMS = _BACKED_BY.replace("'vc'", "'ecosystem'")

MIGRATIONS: list[list] = [
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
    [
        _repair_websites,
    ],
    [
        # What a board's fetch cost, and anything worth saying about it
        # (stopped early, descriptions left for the next run).
        "ALTER TABLE run_boards ADD COLUMN requests INTEGER",
        "ALTER TABLE run_boards ADD COLUMN note TEXT NOT NULL DEFAULT ''",
        _drop_false_workday_findings,
    ],
    [
        """
        -- Portfolio job boards registered as a source of new companies.
        -- Personal data: the list lives here, never in the repository.
        CREATE TABLE portfolio_boards (
            id               INTEGER PRIMARY KEY,
            url              TEXT NOT NULL UNIQUE,     -- scheme and host, e.g. https://jobs.example.vc
            name             TEXT NOT NULL DEFAULT '', -- from the board itself, or given by hand
            platform         TEXT NOT NULL DEFAULT '', -- filled at the first harvest
            added_at         TEXT NOT NULL,
            last_harvest_at  TEXT,
            last_error       TEXT,
            companies_listed INTEGER
        )
        """,
        """
        -- Companies found on portfolio boards. One row per website domain,
        -- however many boards list the company. No role is ever stored here:
        -- only how many of its open roles matched the location rule.
        CREATE TABLE candidates (
            id               INTEGER PRIMARY KEY,
            domain           TEXT NOT NULL UNIQUE,     -- registrable domain: the identity
            name             TEXT NOT NULL DEFAULT '',
            website          TEXT NOT NULL DEFAULT '',
            stage            TEXT,
            employees        INTEGER,
            sectors          TEXT NOT NULL DEFAULT '[]',   -- JSON array
            locations        TEXT NOT NULL DEFAULT '[]',   -- JSON array
            company_id       INTEGER REFERENCES companies(id) ON DELETE SET NULL,  -- the tracked company it is
            status           TEXT NOT NULL DEFAULT 'review',  -- eligible | review | rejected
            reason           TEXT NOT NULL DEFAULT '',
            checks           TEXT NOT NULL DEFAULT '{}',   -- JSON: each rule's result
            matching_roles   INTEGER,                      -- open roles matching the location rule
            roles_rule       TEXT,                         -- the locations that count was taken for
            roles_checked_at TEXT,
            first_seen_at    TEXT NOT NULL,
            last_checked_at  TEXT,                         -- when its company page was last read
            activated_at     TEXT,
            dismissed_at     TEXT,
            dismiss_note     TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE candidate_listings (
            candidate_id  INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
            board_id      INTEGER NOT NULL REFERENCES portfolio_boards(id) ON DELETE CASCADE,
            slug          TEXT NOT NULL,     -- the company's page on that board
            platform_id   TEXT NOT NULL DEFAULT '',  -- the board platform's id for the company
            listed_at     TEXT NOT NULL,
            last_listed_at TEXT NOT NULL,
            PRIMARY KEY (candidate_id, board_id)
        )
        """,
        "CREATE INDEX candidate_listings_board ON candidate_listings (board_id, slug)",
        """
        -- Board pages that could not be turned into a candidate (no domain,
        -- unreadable), so a repeat harvest does not re-read them every time.
        CREATE TABLE portfolio_skips (
            board_id    INTEGER NOT NULL REFERENCES portfolio_boards(id) ON DELETE CASCADE,
            slug        TEXT NOT NULL,
            reason      TEXT NOT NULL,
            checked_at  TEXT NOT NULL,
            PRIMARY KEY (board_id, slug)
        )
        """,
    ],
    [
        """
        -- Jobs in progress, so another process can say what is running.
        -- A row is removed when its job ends normally (see rolescout.jobs).
        CREATE TABLE jobs (
            id          INTEGER PRIMARY KEY,
            kind        TEXT NOT NULL,             -- daily | scout | resolve | harvest
            pid         INTEGER NOT NULL,          -- the process doing it
            started_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,             -- last progress written
            done        INTEGER NOT NULL DEFAULT 0,
            total       INTEGER,                   -- NULL when not known in advance
            unit        TEXT NOT NULL DEFAULT '',  -- what done and total count: boards, companies
            step        TEXT NOT NULL DEFAULT '',  -- for a job made of steps: the current one
            ended_at    TEXT,                      -- set only when it stopped on an error
            error       TEXT
        )
        """,
    ],
    [
        """
        -- Investors and ecosystem groups. An ecosystem group (a city's tech
        -- association) lists companies it did not invest in, so it is kept
        -- out of "Backed by".
        CREATE TABLE vcs (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            short_name  TEXT NOT NULL,             -- what "Backed by" shows
            type        TEXT NOT NULL DEFAULT 'vc' CHECK (type IN ('vc', 'ecosystem')),
            website     TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        )
        """,
        # A VC has zero or more portfolio boards; a board belongs to at most one VC.
        "ALTER TABLE portfolio_boards ADD COLUMN vc_id INTEGER REFERENCES vcs(id) ON DELETE SET NULL",
        # What a board's company page says about the company, kept for its profile.
        "ALTER TABLE candidates ADD COLUMN description TEXT NOT NULL DEFAULT ''",
        """
        -- Which VC is linked to which tracked company, and how we know.
        CREATE TABLE company_vcs (
            company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            vc_id       INTEGER NOT NULL REFERENCES vcs(id) ON DELETE CASCADE,
            source      TEXT NOT NULL CHECK (source IN ('board', 'manual')),
            linked_at   TEXT NOT NULL,
            PRIMARY KEY (company_id, vc_id)
        )
        """,
        "CREATE INDEX company_vcs_vc ON company_vcs (vc_id)",
        """
        -- One profile per tracked company. Each field has the source that set
        -- it (board | agent | manual) and when; a manual value is never
        -- replaced by board or agent data.
        CREATE TABLE company_profiles (
            company_id          INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
            description         TEXT,
            description_source  TEXT,
            description_set_at  TEXT,
            sectors             TEXT NOT NULL DEFAULT '[]',   -- JSON array, from the controlled list
            sectors_source      TEXT,
            sectors_set_at      TEXT,
            sector_tags         TEXT NOT NULL DEFAULT '[]',   -- JSON array: the board's raw tags, all kept
            stage               TEXT,                         -- one of the fixed list (rolescout.profiles.STAGES)
            stage_raw           TEXT,                         -- the value as the board or the person gave it
            stage_source        TEXT,
            stage_set_at        TEXT,
            employees           INTEGER,
            employees_source    TEXT,
            employees_set_at    TEXT,
            hq                  TEXT,
            hq_source           TEXT,
            hq_set_at           TEXT
        )
        """,
        _vcs_and_profiles_from_what_is_stored,
        # --- read-only views for browsing ---------------------------------
        f"""
        CREATE VIEW v_company_overview AS
        SELECT c.id AS company_id, c.name AS company, c.website, c.active,
               p.stage, p.sectors, p.employees, p.hq, p.description,
               {_BACKED_BY} AS backed_by,
               {_ECOSYSTEMS} AS ecosystems,
               (SELECT COUNT(*) FROM roles r WHERE r.company_id = c.id AND r.closed_at IS NULL) AS open_roles,
               (SELECT COALESCE(SUM(s.opened), 0) FROM company_snapshots s WHERE s.company_id = c.id
                AND s.taken_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days')) AS opened_30d,
               (SELECT COALESCE(SUM(s.closed), 0) FROM company_snapshots s WHERE s.company_id = c.id
                AND s.taken_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days')) AS closed_30d,
               (SELECT group_concat(b.source || '/' || b.ref, ', ') FROM boards b WHERE b.company_id = c.id) AS job_boards,
               p.stage_raw, p.sector_tags
        FROM companies c LEFT JOIN company_profiles p ON p.company_id = c.id
        """,
        f"""
        CREATE VIEW v_open_roles AS
        SELECT r.id AS role_id, c.name AS company, r.title, r.department, r.location, r.workplace_type,
               r.date_posted, r.first_seen_at, r.compensation_text, r.url,
               p.stage, p.sectors, p.employees, p.hq,
               {_BACKED_BY} AS backed_by,
               c.id AS company_id
        FROM roles r JOIN companies c ON c.id = r.company_id
        LEFT JOIN company_profiles p ON p.company_id = c.id
        WHERE r.closed_at IS NULL AND c.active = 1
        """,
        """
        -- One row per company and sector, for cuts by sector.
        CREATE VIEW v_company_sectors AS
        SELECT p.company_id, c.name AS company, j.value AS sector, p.stage
        FROM company_profiles p JOIN companies c ON c.id = p.company_id, json_each(p.sectors) j
        """,
        """
        -- Hiring per company per week (weeks start on Monday). Roles of a
        -- company's first fetch are not counted as opened.
        CREATE VIEW v_hiring_weekly AS
        SELECT c.name AS company, date(substr(s.taken_at, 1, 10), 'weekday 0', '-6 days') AS week_start,
               SUM(s.opened) AS opened, SUM(s.closed) AS closed, SUM(s.opened) - SUM(s.closed) AS net,
               s.open_roles AS open_roles_at_week_end, MAX(s.run_id) AS last_run_id, COUNT(*) AS scouts,
               c.id AS company_id
        FROM company_snapshots s JOIN companies c ON c.id = s.company_id
        GROUP BY s.company_id, week_start
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
            for step in MIGRATIONS[version]:
                if callable(step):
                    step(conn)
                else:
                    conn.execute(step)
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
    conn.row_factory = sqlite3.Row  # before migrate(): migration steps read rows by name
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn
