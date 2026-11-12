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

    companies.set_mapping(conn, "Acme", "greenhouse", "acme")
    companies.set_active(conn, "Bolt", False)
    csv_path.write_text("company_name,website,tier\nacme,https://acme.io,3\nCobalt,,\n")
    assert companies.import_csv(conn, csv_path) == (1, 1)

    listed = by_name(conn)
    assert set(listed) == {"Acme", "Bolt", "Cobalt"}
    assert (listed["Acme"]["website"], listed["Acme"]["tier"]) == ("https://acme.io", 3)
    assert (listed["Acme"]["source"], listed["Acme"]["ref"]) == ("greenhouse", "acme")  # kept
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


def test_mapping_rules(conn):
    companies.add_company(conn, "Acme")
    companies.add_company(conn, "Bolt")
    companies.set_mapping(conn, "Acme", "greenhouse", "acme")
    companies.set_mapping(conn, "Acme", "lever", "acme-co")  # remap replaces
    assert (by_name(conn)["Acme"]["source"], by_name(conn)["Acme"]["ref"]) == ("lever", "acme-co")
    assert conn.execute("SELECT COUNT(*) FROM company_sources").fetchone()[0] == 1

    with pytest.raises(CompanyError, match="already mapped to 'Acme'"):
        companies.set_mapping(conn, "Bolt", "lever", "acme-co")
    with pytest.raises(CompanyError, match="unknown source"):
        companies.set_mapping(conn, "Bolt", "workday", "bolt")
    assert by_name(conn)["Bolt"]["source"] is None
