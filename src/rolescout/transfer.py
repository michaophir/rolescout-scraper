"""Export the company list and board mappings from one database into another,
with what was entered by hand: profile values, the VC list and VC links.

The export is one JSON file. It holds your target companies, so it is
personal data: keep it out of the repository.
"""

import json
import sqlite3

from rolescout.adapters import ADAPTERS
from rolescout.db import transaction, utc_now
from rolescout.urls import InvalidWebsite, normalize_website

FORMAT = "rolescout-companies"
FORMAT_VERSION = 1
PROFILE_FIELDS = ("description", "sectors", "stage", "employees", "hq")


class TransferError(Exception):
    """The file is not a rolescout company export this version can read."""


def export_companies(conn: sqlite3.Connection) -> dict:
    """Every company with its boards, its last resolve finding, the profile
    values set by hand and the VCs linked by hand; and the VC list. No roles,
    no runs. Profile values and links that boards supply are not exported:
    the other machine gets those from its own harvest."""
    boards: dict[int, list[dict]] = {}
    for b in conn.execute("SELECT company_id, source, ref, origin, evidence FROM boards ORDER BY id"):
        board = dict(b)
        boards.setdefault(board.pop("company_id"), []).append(board)
    resolutions = {r["company_id"]: r for r in conn.execute("SELECT * FROM company_resolutions")}

    by_hand: dict[int, dict] = {}
    for p in conn.execute("SELECT * FROM company_profiles"):
        kept = {}
        for field in PROFILE_FIELDS:
            if p[f"{field}_source"] == "manual":
                value = json.loads(p[field]) if field == "sectors" else p[field]
                kept[field] = {"value": value, "set_at": p[f"{field}_set_at"]}
                if field == "stage":
                    kept[field]["raw"] = p["stage_raw"]
        if kept:
            by_hand[p["company_id"]] = kept
    linked: dict[int, list[dict]] = {}
    for l in conn.execute("SELECT l.company_id, v.name, l.linked_at FROM company_vcs l JOIN vcs v "
                          "ON v.id = l.vc_id WHERE l.source = 'manual' ORDER BY v.name COLLATE NOCASE"):
        linked.setdefault(l["company_id"], []).append({"vc": l["name"], "linked_at": l["linked_at"]})
    unlinked: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT r.company_id, v.name, r.removed_at FROM company_vc_removals r JOIN vcs v "
                          "ON v.id = r.vc_id ORDER BY v.name COLLATE NOCASE"):
        unlinked.setdefault(r["company_id"], []).append({"vc": r["name"], "removed_at": r["removed_at"]})
    board_urls: dict[int, list[str]] = {}
    for b in conn.execute("SELECT vc_id, url FROM portfolio_boards WHERE vc_id IS NOT NULL ORDER BY id"):
        board_urls.setdefault(b["vc_id"], []).append(b["url"])
    vc_list = [{"name": v["name"], "short_name": v["short_name"], "type": v["type"], "website": v["website"],
                "boards": board_urls.get(v["id"], [])}
               for v in conn.execute("SELECT * FROM vcs ORDER BY name COLLATE NOCASE")]

    companies = []
    for c in conn.execute("SELECT * FROM companies ORDER BY name COLLATE NOCASE"):
        entry = {
            "name": c["name"], "website": c["website"],
            "tags": json.loads(c["tags"]), "active": bool(c["active"]),
            "boards": boards.get(c["id"], []),
        }
        r = resolutions.get(c["id"])
        if r is not None:
            entry["resolution"] = {
                "resolved_at": r["resolved_at"], "outcome": r["outcome"],
                "job_system": r["job_system"], "job_system_url": r["job_system_url"],
                "careers_url": r["careers_url"], "evidence": json.loads(r["evidence"]),
                "manual": bool(r["manual"])}
        if c["id"] in by_hand:
            entry["profile"] = by_hand[c["id"]]
        if c["id"] in linked:
            entry["vcs"] = linked[c["id"]]
        if c["id"] in unlinked:
            entry["vcs_removed"] = unlinked[c["id"]]
        companies.append(entry)
    return {"format": FORMAT, "version": FORMAT_VERSION, "exported_at": utc_now(),
            "vcs": vc_list, "companies": companies}


def import_companies(conn: sqlite3.Connection, data: object) -> dict:
    """Merge an export into this database. Nothing is ever removed.

    Companies are matched by name: new ones are added, existing ones take the
    file's website, tags and active flag. Boards the company does not
    have yet are added. A board already owned by a different company here is
    skipped and reported. VCs are matched by name too: new ones are added,
    existing ones take the file's short name, type and website; a portfolio
    board registered here is attached to the VC the file gives it. Profile
    values and VC links the file marks as made by hand are set as made by
    hand here. All or nothing: a bad file changes nothing.
    """
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise TransferError("not a rolescout company export")
    if data.get("version") != FORMAT_VERSION:
        raise TransferError(
            f"export is format version {data.get('version')}; this rolescout reads "
            f"version {FORMAT_VERSION}. Update rolescout on both machines and export again")

    result = {"companies_added": 0, "companies_updated": 0, "boards_added": 0, "warnings": [],
              "vcs_added": 0, "vcs_updated": 0, "profile_values": 0, "vc_links": 0}
    now = utc_now()
    try:
        with transaction(conn):
            vc_ids: dict[str, int] = {}
            for vc in data.get("vcs") or []:
                vc_name = (vc.get("name") or "").strip()
                if not vc_name:
                    raise TransferError("export contains a VC with no name")
                kind = vc.get("type") if vc.get("type") in ("vc", "ecosystem") else "vc"
                short = (vc.get("short_name") or "").strip() or vc_name
                row = conn.execute("SELECT id FROM vcs WHERE name = ? COLLATE NOCASE", (vc_name,)).fetchone()
                if row is None:
                    vc_id = conn.execute(
                        "INSERT INTO vcs (name, short_name, type, website, created_at) VALUES (?, ?, ?, ?, ?)",
                        (vc_name, short, kind, vc.get("website") or "", now)).lastrowid
                    result["vcs_added"] += 1
                else:
                    vc_id = row["id"]
                    conn.execute("UPDATE vcs SET short_name = ?, type = ?, website = ? WHERE id = ?",
                                 (short, kind, vc.get("website") or "", vc_id))
                    result["vcs_updated"] += 1
                vc_ids[vc_name.casefold()] = vc_id
                for url in vc.get("boards") or []:
                    conn.execute("UPDATE portfolio_boards SET vc_id = ? WHERE url = ?", (vc_id, url))
            for entry in data.get("companies") or []:
                name = (entry.get("name") or "").strip()
                if not name:
                    raise TransferError("export contains a company with no name")
                try:
                    website = normalize_website(entry.get("website"))
                except InvalidWebsite as exc:
                    raise TransferError(f"{name}: {exc}") from None
                fields = (website, json.dumps(sorted(entry.get("tags") or [])),
                          int(bool(entry.get("active", True))))
                row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
                if row is None:
                    company_id = conn.execute(
                        "INSERT INTO companies (website, tags, active, name, created_at) "
                        "VALUES (?, ?, ?, ?, ?)", (*fields, name, now)).lastrowid
                    result["companies_added"] += 1
                else:
                    company_id = row["id"]
                    conn.execute(
                        "UPDATE companies SET website = ?, tags = ?, active = ? "
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

                for field, held in (entry.get("profile") or {}).items():
                    if field not in PROFILE_FIELDS or not isinstance(held, dict) or held.get("value") in (None, "", []):
                        continue
                    value = held["value"]
                    conn.execute("INSERT OR IGNORE INTO company_profiles (company_id) VALUES (?)", (company_id,))
                    extra = ", stage_raw = ?" if field == "stage" else ""
                    conn.execute(
                        f"UPDATE company_profiles SET {field} = ?{extra}, {field}_source = 'manual', "
                        f"{field}_set_at = ? WHERE company_id = ?",
                        (json.dumps(value, ensure_ascii=False) if field == "sectors" else value,
                         *([held.get("raw") or value] if field == "stage" else []),
                         held.get("set_at") or now, company_id))
                    result["profile_values"] += 1
                for link in entry.get("vcs") or []:
                    vc_id = vc_ids.get(str(link.get("vc") or "").casefold())
                    if vc_id is None:
                        result["warnings"].append(f"{name}: skipped link to {link.get('vc')!r}, a VC the file does not list")
                        continue
                    conn.execute("INSERT OR REPLACE INTO company_vcs (company_id, vc_id, source, linked_at) "
                                 "VALUES (?, ?, 'manual', ?)", (company_id, vc_id, link.get("linked_at") or now))
                    conn.execute("DELETE FROM company_vc_removals WHERE company_id = ? AND vc_id = ?",
                                 (company_id, vc_id))
                    result["vc_links"] += 1
                for gone in entry.get("vcs_removed") or []:
                    vc_id = vc_ids.get(str(gone.get("vc") or "").casefold())
                    if vc_id is None:
                        continue
                    conn.execute("INSERT OR REPLACE INTO company_vc_removals (company_id, vc_id, removed_at) "
                                 "VALUES (?, ?, ?)", (company_id, vc_id, gone.get("removed_at") or now))
                    conn.execute("DELETE FROM company_vcs WHERE company_id = ? AND vc_id = ?", (company_id, vc_id))

                r = entry.get("resolution")
                if r:
                    conn.execute(
                        "INSERT OR REPLACE INTO company_resolutions (company_id, resolved_at, outcome, "
                        "job_system, job_system_url, careers_url, evidence, manual) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (company_id, r.get("resolved_at") or now, r.get("outcome") or "unresolved",
                         r.get("job_system"), r.get("job_system_url"), r.get("careers_url"),
                         json.dumps(r.get("evidence") or {"candidates": [], "notes": []},
                                    ensure_ascii=False), int(bool(r.get("manual")))))
    except (AttributeError, TypeError, sqlite3.Error) as exc:
        raise TransferError(f"export file is malformed: {exc}") from exc
    from rolescout import profiles      # profiles builds on the tables this module fills
    with transaction(conn):
        profiles.backfill(conn)         # board-made links follow the boards' VCs; no network
    return result
