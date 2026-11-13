"""Target companies and their source mappings."""

import csv
import json
import sqlite3
from pathlib import Path

from rolescout.adapters import ADAPTERS
from rolescout.db import transaction, utc_now
from rolescout.urls import InvalidWebsite, normalize_website


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


def clean_website(value: object, company: str = "") -> str:
    """``normalize_website`` with the error turned into a CompanyError that names the company."""
    try:
        return normalize_website(value)
    except InvalidWebsite as exc:
        raise CompanyError(f"{company}: {exc}" if company else str(exc)) from None


def get_company(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM companies WHERE name = ?", (name.strip(),)).fetchone()
    if row is None:
        raise CompanyError(f"no company named {name!r}")
    return row


def add_company(conn, name: str, *, website: str = "", tier=None, tags=None) -> int:
    name = name.strip()
    if not name:
        raise CompanyError("company name is empty")
    website = clean_website(website)
    try:
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO companies (name, website, tier, tags, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, website, _parse_tier(tier), json.dumps(_clean_tags(tags)), utc_now()))
    except sqlite3.IntegrityError:
        raise CompanyError(f"company {name!r} already exists") from None
    return cur.lastrowid


def import_csv(conn, path: str | Path) -> tuple[int, int]:
    """Import a target_company_list.csv-style file (company_name, website, tier).

    New names are added. Existing names get their website and tier updated;
    their tags, active flag and mapping are left alone. Returns (added, updated).
    Websites are normalized ("acme.com" becomes "https://acme.com"). A row
    with a value that cannot be a website stops the import before anything
    is written.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if "company_name" not in (reader.fieldnames or []):
            raise CompanyError(f"{path}: expected a 'company_name' column")
        rows = [r for r in reader if (r.get("company_name") or "").strip()]
    # Check every row first, so a bad one changes nothing.
    websites = [clean_website(r.get("website"), f"{path}: {r['company_name'].strip()}") for r in rows]

    added = updated = 0
    now = utc_now()
    with transaction(conn):
        for row, website in zip(rows, websites):
            name = row["company_name"].strip()
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


def add_board(conn, name: str, source: str, ref: str, *, origin: str = "manual",
              evidence: str = "") -> int:
    """Map a company to a board. A company may have several; a board has one owner."""
    company = get_company(conn, name)
    ref = ref.strip()
    if source not in ADAPTERS:
        raise CompanyError(f"unknown source {source!r}; expected one of {', '.join(sorted(ADAPTERS))}")
    if not ref:
        raise CompanyError("board reference is empty")
    owner = conn.execute(
        "SELECT c.name FROM boards b JOIN companies c ON c.id = b.company_id "
        "WHERE b.source = ? AND b.ref = ?", (source, ref)).fetchone()
    if owner is not None:
        raise CompanyError(f"{source}/{ref} is already mapped to {owner['name']!r}")
    with transaction(conn):
        return conn.execute(
            "INSERT INTO boards (company_id, source, ref, origin, evidence, mapped_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (company["id"], source, ref, origin, evidence, utc_now())).lastrowid


def remove_board(conn, name: str, source: str, ref: str) -> int:
    """Unmap a board. Its open roles are closed now, since nothing will fetch
    them again; their one-line rows stay. Returns how many roles were closed.
    """
    company = get_company(conn, name)
    board = conn.execute(
        "SELECT id FROM boards WHERE company_id = ? AND source = ? AND ref = ?",
        (company["id"], source, ref.strip())).fetchone()
    if board is None:
        raise CompanyError(f"{company['name']} has no board {source}/{ref.strip()}")
    now = utc_now()
    with transaction(conn):
        conn.execute(
            "DELETE FROM role_descriptions WHERE role_id IN "
            "(SELECT id FROM roles WHERE board_id = ? AND closed_at IS NULL) AND role_id NOT IN "
            "(SELECT role_id FROM role_marks WHERE status IN ('saved', 'applied'))", (board["id"],))
        closed = conn.execute(
            "UPDATE roles SET closed_at = ? WHERE board_id = ? AND closed_at IS NULL",
            (now, board["id"])).rowcount
        conn.execute("DELETE FROM boards WHERE id = ?", (board["id"],))
    return closed


def list_companies(conn, *, active_only: bool = False) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.id, c.name, c.website, c.tier, c.tags, c.active,
               (SELECT COUNT(*) FROM roles r WHERE r.company_id = c.id AND r.closed_at IS NULL)
                   AS open_roles
        FROM companies c
        WHERE c.active = 1 OR NOT ?
        ORDER BY c.tier IS NULL, c.tier, c.name
        """, (active_only,)).fetchall()
    boards: dict[int, list[dict]] = {}
    for b in conn.execute(
            "SELECT company_id, source, ref, origin, evidence, last_success_at, last_error, "
            "last_error_at FROM boards ORDER BY id"):
        board = dict(b)
        boards.setdefault(board.pop("company_id"), []).append(board)
    return [{**{k: r[k] for k in ("name", "website", "tier", "open_roles")},
             "tags": json.loads(r["tags"]), "active": bool(r["active"]),
             "boards": boards.get(r["id"], [])} for r in rows]
