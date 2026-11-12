"""Export the company list and board mappings from one database into another.

The export is one JSON file. It holds your target companies, so it is
personal data: keep it out of the repository.
"""

import json
import sqlite3

from rolescout.adapters import ADAPTERS
from rolescout.db import transaction, utc_now

FORMAT = "rolescout-companies"
FORMAT_VERSION = 1


class TransferError(Exception):
    """The file is not a rolescout company export this version can read."""


def export_companies(conn: sqlite3.Connection) -> dict:
    """Every company with its boards and its last resolve finding. No roles, no runs."""
    boards: dict[int, list[dict]] = {}
    for b in conn.execute("SELECT company_id, source, ref, origin, evidence FROM boards ORDER BY id"):
        board = dict(b)
        boards.setdefault(board.pop("company_id"), []).append(board)
    resolutions = {r["company_id"]: r for r in conn.execute("SELECT * FROM company_resolutions")}

    companies = []
    for c in conn.execute("SELECT * FROM companies ORDER BY tier IS NULL, tier, name"):
        entry = {
            "name": c["name"], "website": c["website"], "tier": c["tier"],
            "tags": json.loads(c["tags"]), "active": bool(c["active"]),
            "boards": boards.get(c["id"], []),
        }
        r = resolutions.get(c["id"])
        if r is not None:
            entry["resolution"] = {
                "resolved_at": r["resolved_at"], "outcome": r["outcome"],
                "job_system": r["job_system"], "job_system_url": r["job_system_url"],
                "careers_url": r["careers_url"], "evidence": json.loads(r["evidence"])}
        companies.append(entry)
    return {"format": FORMAT, "version": FORMAT_VERSION, "exported_at": utc_now(),
            "companies": companies}


def import_companies(conn: sqlite3.Connection, data: object) -> dict:
    """Merge an export into this database. Nothing is ever removed.

    Companies are matched by name: new ones are added, existing ones take the
    file's website, tier, tags and active flag. Boards the company does not
    have yet are added. A board already owned by a different company here is
    skipped and reported. All or nothing: a bad file changes nothing.
    """
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise TransferError("not a rolescout company export")
    if data.get("version") != FORMAT_VERSION:
        raise TransferError(
            f"export is format version {data.get('version')}; this rolescout reads "
            f"version {FORMAT_VERSION}. Update rolescout on both machines and export again")

    result = {"companies_added": 0, "companies_updated": 0, "boards_added": 0, "warnings": []}
    now = utc_now()
    try:
        with transaction(conn):
            for entry in data.get("companies") or []:
                name = (entry.get("name") or "").strip()
                if not name:
                    raise TransferError("export contains a company with no name")
                fields = (entry.get("website") or "", entry.get("tier"),
                          json.dumps(sorted(entry.get("tags") or [])),
                          int(bool(entry.get("active", True))))
                row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
                if row is None:
                    company_id = conn.execute(
                        "INSERT INTO companies (website, tier, tags, active, name, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (*fields, name, now)).lastrowid
                    result["companies_added"] += 1
                else:
                    company_id = row["id"]
                    conn.execute(
                        "UPDATE companies SET website = ?, tier = ?, tags = ?, active = ? "
                        "WHERE id = ?", (*fields, company_id))
                    result["companies_updated"] += 1

                for board in entry.get("boards") or []:
                    source, ref = board.get("source"), (board.get("ref") or "").strip()
                    if source not in ADAPTERS or not ref:
                        result["warnings"].append(
                            f"{name}: skipped board {source}/{ref} (source not supported here)")
                        continue
                    owner = conn.execute(
                        "SELECT c.id, c.name FROM boards b JOIN companies c ON c.id = b.company_id "
                        "WHERE b.source = ? AND b.ref = ?", (source, ref)).fetchone()
                    if owner is None:
                        conn.execute(
                            "INSERT INTO boards (company_id, source, ref, origin, evidence, mapped_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (company_id, source, ref, board.get("origin") or "manual",
                             board.get("evidence") or "", now))
                        result["boards_added"] += 1
                    elif owner["id"] != company_id:
                        result["warnings"].append(
                            f"{name}: skipped board {source}/{ref}, already mapped to {owner['name']!r} here")

                r = entry.get("resolution")
                if r:
                    conn.execute(
                        "INSERT OR REPLACE INTO company_resolutions (company_id, resolved_at, outcome, "
                        "job_system, job_system_url, careers_url, evidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (company_id, r.get("resolved_at") or now, r.get("outcome") or "unresolved",
                         r.get("job_system"), r.get("job_system_url"), r.get("careers_url"),
                         json.dumps(r.get("evidence") or {"candidates": [], "notes": []},
                                    ensure_ascii=False)))
    except (AttributeError, TypeError, sqlite3.Error) as exc:
        raise TransferError(f"export file is malformed: {exc}") from exc
    return result
