import sqlite3
from pathlib import Path

import pytest

from rolescout import companies, db


def test_path_precedence_flag_then_env_then_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROLESCOUT_DB", "/from/env.db")
    assert db.resolve_db_path("/from/flag.db") == Path("/from/flag.db")
    assert db.resolve_db_path() == Path("/from/env.db")
    monkeypatch.delenv("ROLESCOUT_DB")
    assert db.resolve_db_path() == tmp_path / ".rolescout" / "rolescout.db"


def test_connect_creates_versioned_wal_database(tmp_path):
    path = tmp_path / "nested" / "dir" / "r.db"
    conn = db.connect(path)
    assert path.exists()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.schema_version(conn) == db.SCHEMA_VERSION >= 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables >= {"companies", "boards", "roles", "role_descriptions", "runs",
                      "run_boards", "company_snapshots", "company_resolutions"}
    conn.close()
    db.connect(path).close()  # reopening an up-to-date database is a no-op


def test_migrations_apply_in_order_and_only_once(tmp_path, monkeypatch):
    path = tmp_path / "r.db"
    db.connect(path).close()
    extra = db.MIGRATIONS + [["ALTER TABLE companies ADD COLUMN notes TEXT"]]
    monkeypatch.setattr(db, "MIGRATIONS", extra)
    monkeypatch.setattr(db, "SCHEMA_VERSION", len(extra))
    conn = db.connect(path)
    assert db.schema_version(conn) == len(extra)
    assert "notes" in {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    conn.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "r.db"
    db.connect(path).close()
    extra = db.MIGRATIONS + [["CREATE TABLE half_done (x)", "THIS IS NOT SQL"]]
    monkeypatch.setattr(db, "MIGRATIONS", extra)
    monkeypatch.setattr(db, "SCHEMA_VERSION", len(extra))
    with pytest.raises(sqlite3.OperationalError):
        db.connect(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == len(extra) - 1
    assert raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'half_done'").fetchone()[0] == 0


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "r.db"
    conn = db.connect(path)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(db.SchemaError):
        db.connect(path)


def test_upgrade_clears_compensation_that_is_not_pay(isolated_db_path):
    conn = db.connect()
    company_id = companies.add_company(conn, "Acme")
    board_id = companies.add_board(conn, "Acme", "lever", "acme")
    stored = {
        ("lever", "eeo"): "We are an equal opportunity employer and value diversity.",
        ("lever", "benefits"): "401(k) match, 25 days off and the chance to work with 300 colleagues.",
        ("lever", "range"): "The base range for this position is $87,069-$124,385 plus equity. Benefits include",
        ("ashby", "words"): "Offers Equity",
        ("ashby", "figure"): "$150K – $200K • Offers Equity",
        ("greenhouse", "notice"): "We review pay every year.",
        ("greenhouse", "pay"): "$190,000 — $230,000 USD",
        ("rippling", "pay"): "USD 185,000 - 240,000 per year (US)",
        ("workday", "none"): "",
    }
    for (source, job_id), text in stored.items():
        conn.execute("INSERT INTO roles (source, source_job_id, company_id, board_id, title, compensation_text, "
                     "first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, 'Engineer', ?, 'x', 'x')",
                     (source, job_id, company_id, board_id, text))
    # As the database was before this version.
    conn.execute("DROP TABLE shortlist_postings")
    conn.execute("PRAGMA user_version = 11")
    conn.close()

    upgraded = db.connect()
    assert db.schema_version(upgraded) == db.SCHEMA_VERSION >= 12
    kept = {(r["source"], r["source_job_id"]): r["compensation_text"]
            for r in upgraded.execute("SELECT source, source_job_id, compensation_text FROM roles")}
    # Text with no number goes, from any source. Lever's goes whatever it
    # says: it was the posting's closing section, and the next fetch stores
    # pay from the pay fields.
    assert {key for key, text in kept.items() if text} == {("ashby", "figure"), ("greenhouse", "pay"), ("rippling", "pay")}
    assert all(kept[key] == stored[key] for key in kept if kept[key])
    assert upgraded.execute("SELECT COUNT(*) FROM shortlist_postings").fetchone()[0] == 0
