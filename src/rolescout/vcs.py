"""The VC list: investors and ecosystem groups, their portfolio boards, and
which companies each is linked to.

A link comes from a board (the VC's portfolio board lists the company) or is
made by hand. "Backed by" reads these links, shows each VC's short name, and
leaves out ecosystem groups: a city's tech association lists companies it
never invested in.
"""

import re
import sqlite3

from rolescout.companies import CompanyError, clean_website, get_company
from rolescout.db import transaction, utc_now

TYPES = ("vc", "ecosystem")
# Words dropped from the end of a name to make its short form.
_TAIL = {"ventures", "venture", "partners", "capital", "management", "vc", "fund", "funds", "investments",
         "jobs", "job", "board", "talent", "network", "careers", "portfolio", "companies", "group",
         "llc", "lp", "inc", "the", "and", "&"}


class VcError(Exception):
    """A VC request that cannot be carried out; the message says why."""


def short_name(name: str) -> str:
    """"Acme Venture Partners" is "Acme": the name without the words every
    firm's name ends with. A name made only of such words is kept whole."""
    words = name.split()
    while len(words) > 1 and words[-1].lower().strip(".,") in _TAIL:
        words.pop()
    return " ".join(words) or name.strip()


def _site(value: str, name: str) -> str:
    try:
        return clean_website(value, name)
    except CompanyError as exc:
        raise VcError(str(exc)) from None


def get_vc(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """A VC by id, name or short name."""
    ref = str(ref).strip()
    row = conn.execute("SELECT * FROM vcs WHERE id = ?", (int(ref),)).fetchone() if ref.isdigit() else None
    if row is None:
        row = conn.execute("SELECT * FROM vcs WHERE name = ? COLLATE NOCASE", (ref,)).fetchone()
    if row is None:
        rows = conn.execute("SELECT * FROM vcs WHERE short_name = ? COLLATE NOCASE", (ref,)).fetchall()
        if len(rows) > 1:
            raise VcError(f"{len(rows)} VCs are called {ref!r}; use the full name: "
                          + ", ".join(r["name"] for r in rows))
        row = rows[0] if rows else None
    if row is None:
        raise VcError(f"no VC named {ref!r} (see: rolescout vcs list)")
    return row


def _insert(conn: sqlite3.Connection, name: str, short: str, kind: str, website: str) -> int:
    return conn.execute(
        "INSERT INTO vcs (name, short_name, type, website, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, short, kind, website, utc_now())).lastrowid


def add_vc(conn: sqlite3.Connection, name: str, *, short: str = "", kind: str = "vc", website: str = "") -> sqlite3.Row:
    name = name.strip()
    if not name:
        raise VcError("the VC's name is empty")
    if kind not in TYPES:
        raise VcError(f"type must be one of {', '.join(TYPES)}, got {kind!r}")
    if conn.execute("SELECT 1 FROM vcs WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        raise VcError(f"a VC named {name!r} already exists")
    with transaction(conn):
        vc_id = _insert(conn, name, short.strip() or short_name(name), kind, _site(website, name))
    return get_vc(conn, str(vc_id))


def edit_vc(conn: sqlite3.Connection, ref: str, *, name: str | None = None, short: str | None = None,
            kind: str | None = None, website: str | None = None) -> sqlite3.Row:
    vc = get_vc(conn, ref)
    if kind is not None and kind not in TYPES:
        raise VcError(f"type must be one of {', '.join(TYPES)}, got {kind!r}")
    if name is not None:
        name = name.strip()
        if not name:
            raise VcError("the VC's name is empty")
        clash = conn.execute("SELECT 1 FROM vcs WHERE name = ? COLLATE NOCASE AND id != ?", (name, vc["id"])).fetchone()
        if clash:
            raise VcError(f"a VC named {name!r} already exists")
    with transaction(conn):
        conn.execute(
            "UPDATE vcs SET name = COALESCE(?, name), short_name = COALESCE(?, short_name), "
            "type = COALESCE(?, type), website = COALESCE(?, website) WHERE id = ?",
            (name, short.strip() if short is not None and short.strip() else None, kind,
             _site(website, vc["name"]) if website is not None else None, vc["id"]))
    return get_vc(conn, str(vc["id"]))


def remove_vc(conn: sqlite3.Connection, ref: str) -> tuple[str, int, int]:
    """Remove a VC and its links. Its boards stay registered, attached to no
    VC. Returns (name, links removed, boards detached)."""
    vc = get_vc(conn, ref)
    with transaction(conn):
        links = conn.execute("DELETE FROM company_vcs WHERE vc_id = ?", (vc["id"],)).rowcount
        boards = conn.execute("UPDATE portfolio_boards SET vc_id = NULL WHERE vc_id = ?", (vc["id"],)).rowcount
        conn.execute("DELETE FROM vcs WHERE id = ?", (vc["id"],))
    return vc["name"], links, boards


def list_vcs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT v.*, (SELECT COUNT(*) FROM company_vcs l WHERE l.vc_id = v.id) AS companies, "
        "(SELECT COUNT(*) FROM company_vcs l WHERE l.vc_id = v.id AND l.source = 'manual') AS linked_by_hand "
        "FROM vcs v ORDER BY v.type, v.name COLLATE NOCASE").fetchall()
    boards: dict[int, list[str]] = {}
    for b in conn.execute("SELECT vc_id, url FROM portfolio_boards WHERE vc_id IS NOT NULL ORDER BY id"):
        boards.setdefault(b["vc_id"], []).append(b["url"])
    return [{**dict(r), "boards": boards.get(r["id"], [])} for r in rows]


def _board(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    from rolescout import portfolio      # portfolio registers boards and calls into this module
    try:
        return portfolio.get_board(conn, ref)
    except portfolio.PortfolioError as exc:
        raise VcError(str(exc)) from None


def attach_board(conn: sqlite3.Connection, board_ref: str, vc_ref: str) -> tuple[str, str]:
    """Say which VC a portfolio board belongs to. Returns (board URL, VC name)."""
    board, vc = _board(conn, board_ref), get_vc(conn, vc_ref)
    with transaction(conn):
        conn.execute("UPDATE portfolio_boards SET vc_id = ? WHERE id = ?", (vc["id"], board["id"]))
        sync_board_links(conn)
    return board["url"], vc["name"]


def ensure_for_board(conn: sqlite3.Connection, board_id: int) -> int | None:
    """Give a board that belongs to no VC one made from the board itself: its
    name, or its host until a harvest learns the name. Must run inside a
    transaction. Returns the VC's id."""
    board = conn.execute("SELECT * FROM portfolio_boards WHERE id = ?", (board_id,)).fetchone()
    if board is None:
        return None
    host = board["url"].split("//")[-1]
    name = board["name"].strip() or host
    if board["vc_id"] is not None:
        # Created from the host before the board's own name was known: take the name now.
        vc = conn.execute("SELECT * FROM vcs WHERE id = ?", (board["vc_id"],)).fetchone()
        if vc is not None and vc["name"] == host and name != host and not conn.execute(
                "SELECT 1 FROM vcs WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
            conn.execute("UPDATE vcs SET name = ?, short_name = ? WHERE id = ?", (name, short_name(name), vc["id"]))
        return board["vc_id"]
    existing = conn.execute("SELECT id FROM vcs WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    # The firm's own site is usually the board's host without its first label
    # (jobs.example.vc is example.vc). A guess: correct it with `vcs edit`.
    labels = host.split(".")
    website = "https://" + (".".join(labels[1:]) if len(labels) > 2 else host)
    vc_id = existing["id"] if existing else _insert(conn, name, short_name(name), "vc", website)
    conn.execute("UPDATE portfolio_boards SET vc_id = ? WHERE id = ?", (vc_id, board_id))
    return vc_id


def sync_board_links(conn: sqlite3.Connection) -> None:
    """Make the board-sourced links match what the boards list now: a tracked
    company is linked to a VC when one of that VC's boards lists it. Links
    made by hand are never touched, and a link removed by hand is not made
    again. Must run inside a transaction."""
    # An upgrade runs this from a migration older than the removals table.
    kept_out = ""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'company_vc_removals'").fetchone():
        kept_out = ("AND NOT EXISTS (SELECT 1 FROM company_vc_removals r "
                    "WHERE r.company_id = c.company_id AND r.vc_id = b.vc_id) ")
        conn.execute(
            "DELETE FROM company_vcs WHERE source = 'board' AND EXISTS ("
            "  SELECT 1 FROM company_vc_removals r "
            "  WHERE r.company_id = company_vcs.company_id AND r.vc_id = company_vcs.vc_id)")
    conn.execute(
        "DELETE FROM company_vcs WHERE source = 'board' AND NOT EXISTS ("
        "  SELECT 1 FROM candidates c JOIN candidate_listings l ON l.candidate_id = c.id "
        "  JOIN portfolio_boards b ON b.id = l.board_id "
        "  WHERE c.company_id = company_vcs.company_id AND b.vc_id = company_vcs.vc_id)")
    conn.execute(
        "INSERT OR IGNORE INTO company_vcs (company_id, vc_id, source, linked_at) "
        "SELECT c.company_id, b.vc_id, 'board', MIN(l.listed_at) FROM candidates c "
        "JOIN candidate_listings l ON l.candidate_id = c.id JOIN portfolio_boards b ON b.id = l.board_id "
        f"WHERE c.company_id IS NOT NULL AND b.vc_id IS NOT NULL {kept_out}"
        "GROUP BY c.company_id, b.vc_id")


def link_company(conn: sqlite3.Connection, company: str, vc_ref: str) -> tuple[str, str, bool]:
    """Link a company to a VC by hand. Returns (company, VC, whether it was new)."""
    try:
        row = get_company(conn, company)
    except CompanyError as exc:
        raise VcError(str(exc)) from None
    vc = get_vc(conn, vc_ref)
    with transaction(conn):
        had = conn.execute("SELECT source FROM company_vcs WHERE company_id = ? AND vc_id = ?",
                           (row["id"], vc["id"])).fetchone()
        # By hand outranks a board: the link stays if the board drops the
        # company, and it undoes an earlier removal by hand.
        conn.execute("DELETE FROM company_vc_removals WHERE company_id = ? AND vc_id = ?", (row["id"], vc["id"]))
        conn.execute("INSERT OR REPLACE INTO company_vcs (company_id, vc_id, source, linked_at) "
                     "VALUES (?, ?, 'manual', ?)", (row["id"], vc["id"], utc_now()))
    return row["name"], vc["name"], had is None or had["source"] != "manual"


def unlink_company(conn: sqlite3.Connection, company: str, vc_ref: str) -> tuple[str, str, bool]:
    """Remove a link, however it was made, and record the removal: a board
    that lists the company does not bring it back. Linking the two by hand
    again does. Returns (company, VC, whether one was removed)."""
    try:
        row = get_company(conn, company)
    except CompanyError as exc:
        raise VcError(str(exc)) from None
    vc = get_vc(conn, vc_ref)
    with transaction(conn):
        removed = conn.execute("DELETE FROM company_vcs WHERE company_id = ? AND vc_id = ?",
                               (row["id"], vc["id"])).rowcount
        listed = conn.execute(
            "SELECT 1 FROM candidates c JOIN candidate_listings l ON l.candidate_id = c.id "
            "JOIN portfolio_boards b ON b.id = l.board_id WHERE c.company_id = ? AND b.vc_id = ?",
            (row["id"], vc["id"])).fetchone()
        if removed or listed:
            conn.execute("INSERT OR REPLACE INTO company_vc_removals (company_id, vc_id, removed_at) "
                         "VALUES (?, ?, ?)", (row["id"], vc["id"], utc_now()))
        sync_board_links(conn)
    return row["name"], vc["name"], bool(removed)


def company_links(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    """Every VC and ecosystem group linked to one company."""
    rows = conn.execute(
        "SELECT v.name, v.short_name, v.type, l.source, l.linked_at FROM company_vcs l "
        "JOIN vcs v ON v.id = l.vc_id WHERE l.company_id = ? ORDER BY v.type, v.id", (company_id,))
    return [dict(r) for r in rows]


def removed_links(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    """The VCs whose link to one company was removed by hand."""
    rows = conn.execute(
        "SELECT v.name, v.short_name, r.removed_at FROM company_vc_removals r "
        "JOIN vcs v ON v.id = r.vc_id WHERE r.company_id = ? ORDER BY v.id", (company_id,))
    return [dict(r) for r in rows]
