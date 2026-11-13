import csv
from pathlib import Path

import pytest

from rolescout import companies
from rolescout.companies import CompanyError

REPO_CSV = Path(__file__).parent.parent / "target_company_list.csv"


def by_name(conn):
    return {c["name"]: c for c in companies.list_companies(conn)}


def test_import_csv_adds_then_updates(conn, tmp_path):
    csv_path = tmp_path / "c.csv"
    csv_path.write_text("company_name,website,tier\nAcme,https://acme.com,1\nBolt,https://bolt.com,2\n")
    assert companies.import_csv(conn, csv_path) == (2, 0)

    companies.add_board(conn, "Acme", "greenhouse", "acme")
    companies.set_active(conn, "Bolt", False)
    csv_path.write_text("company_name,website,tier\nacme,https://acme.io,3\nCobalt,,\n")
    assert companies.import_csv(conn, csv_path) == (1, 1)

    listed = by_name(conn)
    assert set(listed) == {"Acme", "Bolt", "Cobalt"}
    assert (listed["Acme"]["website"], listed["Acme"]["tier"]) == ("https://acme.io", 3)
    assert [(b["source"], b["ref"]) for b in listed["Acme"]["boards"]] == [("greenhouse", "acme")]  # kept
    assert listed["Bolt"]["active"] is False  # not reactivated by a re-import
    assert listed["Cobalt"]["tier"] is None


def test_import_the_repo_csv_format(conn):
    with open(REPO_CSV, newline="", encoding="utf-8") as f:
        names = [row["company_name"].strip() for row in csv.DictReader(f)]
    unique = {name.lower() for name in names}
    added, updated = companies.import_csv(conn, REPO_CSV)
    # A name listed twice in the file is added once, then counted as an update.
    assert (added, updated) == (len(unique), len(names) - len(unique))
    assert len(by_name(conn)) == len(unique)
    assert by_name(conn)["Anthropic"]["tier"] == 1


def test_import_rejects_wrong_columns(conn, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("name,url\nAcme,https://acme.com\n")
    with pytest.raises(CompanyError):
        companies.import_csv(conn, bad)


def test_add_deactivate_activate(conn):
    companies.add_company(conn, " Acme ", website="https://acme.com", tier=2, tags=["ai", " b2b ", "ai"])
    acme = by_name(conn)["Acme"]
    assert (acme["tier"], acme["tags"], acme["active"]) == (2, ["ai", "b2b"], True)
    with pytest.raises(CompanyError, match="already exists"):
        companies.add_company(conn, "ACME")

    companies.set_active(conn, "acme", False)
    assert by_name(conn)["Acme"]["active"] is False
    assert companies.list_companies(conn, active_only=True) == []
    companies.set_active(conn, "Acme", True)
    assert by_name(conn)["Acme"]["active"] is True
    with pytest.raises(CompanyError, match="no company named"):
        companies.set_active(conn, "Nope", False)


def test_a_company_can_have_several_boards_but_a_board_has_one_owner(conn):
    companies.add_company(conn, "Acme")
    companies.add_company(conn, "Bolt")
    companies.add_board(conn, "Acme", "greenhouse", "acme")
    companies.add_board(conn, "acme", "lever", "acme-eu", origin="link", evidence="linked from x")
    boards = by_name(conn)["Acme"]["boards"]
    assert [(b["source"], b["ref"], b["origin"]) for b in boards] == [
        ("greenhouse", "acme", "manual"), ("lever", "acme-eu", "link")]
    assert boards[1]["evidence"] == "linked from x"

    with pytest.raises(CompanyError, match="already mapped to 'Acme'"):
        companies.add_board(conn, "Bolt", "lever", "acme-eu")
    with pytest.raises(CompanyError, match="already mapped to 'Acme'"):
        companies.add_board(conn, "Acme", "lever", "acme-eu")
    with pytest.raises(CompanyError, match="unknown source"):
        companies.add_board(conn, "Bolt", "icims", "bolt")
    assert by_name(conn)["Bolt"]["boards"] == []


def test_unmapping_a_board_closes_its_open_roles(conn):
    from rolescout.db import transaction
    from rolescout.scout import apply_fetch
    from tests.test_scout import role

    company_id = companies.add_company(conn, "Acme")
    keep = companies.add_board(conn, "Acme", "greenhouse", "acme")
    drop = companies.add_board(conn, "Acme", "lever", "acme")
    with transaction(conn):
        apply_fetch(conn, company_id, keep, [role("K1")], "2026-01-01T00:00:00Z")
        apply_fetch(conn, company_id, drop, [role("D1", source="lever"), role("D2", source="lever")],
                    "2026-01-01T00:00:00Z")

    assert companies.remove_board(conn, "Acme", "lever", "acme") == 2
    rows = {r["source_job_id"]: r for r in conn.execute("SELECT * FROM roles")}
    assert rows["K1"]["closed_at"] is None
    assert rows["D1"]["closed_at"] and rows["D1"]["board_id"] is None  # row kept, board gone
    assert conn.execute("SELECT COUNT(*) FROM role_descriptions").fetchone()[0] == 1
    assert [b["ref"] for b in by_name(conn)["Acme"]["boards"]] == ["acme"]
    with pytest.raises(CompanyError, match="has no board"):
        companies.remove_board(conn, "Acme", "lever", "acme")
