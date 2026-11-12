import sqlite3
from pathlib import Path

import pytest

from rolescout import db


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
    assert tables >= {"companies", "company_sources", "roles", "role_descriptions",
                      "runs", "run_companies", "company_snapshots"}
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
