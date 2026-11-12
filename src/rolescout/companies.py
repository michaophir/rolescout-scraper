"""Target companies and their source mappings."""

import csv
import json
import sqlite3
from pathlib import Path

from rolescout.adapters import ADAPTERS
from rolescout.db import transaction, utc_now


class CompanyError(Exception):
    """A company command could not be carried out (unknown name, duplicate, ...)."""


def _clean_tags(tags) -> list[str]:
    return sorted({t.strip() for t in tags or [] if t and t.strip()})


def _parse_tier(value) -> int | None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise CompanyError(f"tier must be a whole number, got {text!r}") from None


def get_company(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM companies WHERE name = ?", (name.strip(),)).fetchone()
    if row is None:
        raise CompanyError(f"no company named {name!r}")
    return row


def add_company(conn, name: str, *, website: str = "", tier=None, tags=None) -> int:
    name = name.strip()
    if not name:
        raise CompanyError("company name is empty")
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO companies (name, website, tier, tags, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, website.strip(), _parse_tier(tier), json.dumps(_clean_tags(tags)), utc_now()))
    except sqlite3.IntegrityError:
        raise CompanyError(f"company {name!r} already exists") from None
    return cur.lastrowid


def import_csv(conn, path: str | Path) -> tuple[int, int]:
    """Import a target_company_list.csv-style file (company_name, website, tier).

    New names are added. Existing names get their website and tier updated;
    their tags, active flag and mapping are left alone. Returns (added, updated).
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if "company_name" not in (reader.fieldnames or []):
            raise CompanyError(f"{path}: expected a 'company_name' column")
        rows = [r for r in reader if (r.get("company_name") or "").strip()]

    added = updated = 0
    now = utc_now()
    with transaction(conn):
        for row in rows:
            name = row["company_name"].strip()
            website = (row.get("website") or "").strip()
            tier = _parse_tier(row.get("tier"))
            cur = conn.execute(
                "UPDATE companies SET website = ?, tier = ? WHERE name = ?", (website, tier, name))
            if cur.rowcount:
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO companies (name, website, tier, created_at) VALUES (?, ?, ?, ?)",
                    (name, website, tier, now))
                added += 1
    return added, updated


def set_active(conn, name: str, active: bool) -> None:
    company = get_company(conn, name)
    with transaction(conn):
        conn.execute("UPDATE companies SET active = ? WHERE id = ?", (int(active), company["id"]))


def set_mapping(conn, name: str, source: str, ref: str) -> None:
    """Point a company at a board. Replaces any previous mapping."""
    company = get_company(conn, name)
    ref = ref.strip()
    if source not in ADAPTERS:
        raise CompanyError(f"unknown source {source!r}; expected one of {', '.join(sorted(ADAPTERS))}")
    if not ref:
        raise CompanyError("board reference is empty")
    try:
        with transaction(conn):
            conn.execute("DELETE FROM company_sources WHERE company_id = ?", (company["id"],))
            conn.execute(
                "INSERT INTO company_sources (company_id, source, ref, mapped_at) VALUES (?, ?, ?, ?)",
                (company["id"], source, ref, utc_now()))
    except sqlite3.IntegrityError:
        other = conn.execute(
            "SELECT c.name FROM company_sources s JOIN companies c ON c.id = s.company_id "
            "WHERE s.source = ? AND s.ref = ?", (source, ref)).fetchone()
        raise CompanyError(f"{source}/{ref} is already mapped to {other['name']!r}") from None


def list_companies(conn, *, active_only: bool = False) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.name, c.website, c.tier, c.tags, c.active,
               s.source, s.ref, s.last_success_at, s.last_error, s.last_error_at,
               (SELECT COUNT(*) FROM roles r WHERE r.company_id = c.id AND r.closed_at IS NULL)
                   AS open_roles
        FROM companies c LEFT JOIN company_sources s ON s.company_id = c.id
        WHERE c.active = 1 OR NOT ?
        ORDER BY c.tier IS NULL, c.tier, c.name
        """, (active_only,)).fetchall()
    return [{**dict(r), "tags": json.loads(r["tags"]), "active": bool(r["active"])} for r in rows]
