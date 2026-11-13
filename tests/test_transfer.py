import json

import pytest

from rolescout import cli, companies, db
from rolescout.resolve import Resolution, save_resolution
from rolescout.transfer import TransferError, export_companies, import_companies


@pytest.fixture
def source(conn):
    companies.add_company(conn, "Acme", website="https://acme.com", tier=1, tags=["ai", "b2b"])
    companies.add_company(conn, "Bolt", website="https://bolt.com", tier=2)
    companies.add_company(conn, "Cobalt")
    companies.add_board(conn, "Acme", "greenhouse", "acme", origin="link", evidence="linked from x")
    companies.add_board(conn, "Acme", "lever", "acme-eu")
    companies.add_board(conn, "Cobalt", "ashby", "Cobalt")
    companies.set_active(conn, "Cobalt", False)
    save_resolution(conn, Resolution(
        company="Bolt", outcome="unsupported", job_system="icims",
        job_system_url="https://bolt.icims.com/x", notes=["a note"]))
    return conn


def snapshot(conn):
    """Everything an export is meant to carry, in a comparable form."""
    listed = [{k: c[k] for k in ("name", "website", "tier", "tags", "active")}
              | {"boards": [(b["source"], b["ref"], b["origin"], b["evidence"]) for b in c["boards"]]}
              for c in companies.list_companies(conn)]
    resolutions = [tuple(r) for r in conn.execute(
        "SELECT c.name, r.outcome, r.job_system, r.job_system_url, r.evidence "
        "FROM company_resolutions r JOIN companies c ON c.id = r.company_id ORDER BY c.name")]
    return listed, resolutions


def test_export_import_round_trip(source, tmp_path):
    data = json.loads(json.dumps(export_companies(source)))  # as it would come off disk
    assert (data["format"], data["version"]) == ("rolescout-companies", 1)
    assert [c["name"] for c in data["companies"]] == ["Acme", "Bolt", "Cobalt"]

    target = db.connect(tmp_path / "other.db")
    result = import_companies(target, data)
    assert result == {"companies_added": 3, "companies_updated": 0, "boards_added": 3, "warnings": []}
    assert snapshot(target) == snapshot(source)
    # Mappings arrive unfetched: the first scout on the new machine is a baseline.
    assert [r[0] for r in target.execute("SELECT last_success_at FROM boards")] == [None, None, None]

    # Importing the same file again changes nothing.
    assert import_companies(target, data) == {
        "companies_added": 0, "companies_updated": 3, "boards_added": 0, "warnings": []}
    assert snapshot(target) == snapshot(source)


def test_import_merges_and_never_removes(source, tmp_path):
    data = export_companies(source)
    target = db.connect(tmp_path / "other.db")
    companies.add_company(target, "acme", website="https://old.example", tier=9)   # will be updated
    companies.add_board(target, "acme", "ashby", "acme-local")                     # will be kept
    companies.add_company(target, "Local Only")                                    # will be kept
    companies.add_board(target, "Local Only", "ashby", "Cobalt")                   # conflicts with the file

    result = import_companies(target, data)
    assert (result["companies_added"], result["companies_updated"], result["boards_added"]) == (2, 1, 2)
    assert result["warnings"] == ["Cobalt: skipped board ashby/Cobalt, already mapped to 'Local Only' here"]
    listed = {c["name"]: c for c in companies.list_companies(target)}
    assert (listed["acme"]["website"], listed["acme"]["tier"]) == ("https://acme.com", 1)
    assert [b["ref"] for b in listed["acme"]["boards"]] == ["acme-local", "acme", "acme-eu"]
    assert "Local Only" in listed and listed["Cobalt"]["boards"] == []


def test_bad_files_are_refused_and_change_nothing(conn):
    for bad in ({"companies": []}, [], {"format": "rolescout-companies", "version": 99, "companies": []}):
        with pytest.raises(TransferError):
            import_companies(conn, bad)
    half = {"format": "rolescout-companies", "version": 1,
            "companies": [{"name": "Good"}, {"name": ""}]}
    with pytest.raises(TransferError):
        import_companies(conn, half)
    assert companies.list_companies(conn) == []  # all or nothing


def test_export_and_import_commands(source, tmp_path, capsys, monkeypatch):
    out_file = tmp_path / "private" / "rolescout-companies.json"
    assert cli.main(["companies", "export", str(out_file)]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"Exported 3 companies and 3 boards to {out_file}\n"
    assert captured.err == ""                       # not inside a git repository
    assert out_file.stat().st_mode & 0o777 == 0o600  # readable by you only

    other = tmp_path / "other.db"
    assert cli.main(["companies", "import", str(out_file), "--db", str(other)]) == 0
    assert "3 companies added, 0 updated, 3 boards added." in capsys.readouterr().out
    assert snapshot(db.connect(other)) == snapshot(source)

    # Piping: export - | import -
    assert cli.main(["companies", "export", "-"]) == 0
    piped = capsys.readouterr().out
    third = tmp_path / "third.db"
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(piped))
    assert cli.main(["companies", "import", "-", "--db", str(third)]) == 0
    assert snapshot(db.connect(third)) == snapshot(source)

    # A warning when the export would land inside a git repository.
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert cli.main(["companies", "export", str(repo / "x.json")]) == 0
    assert "is inside the git repository" in capsys.readouterr().err

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert cli.main(["companies", "import", str(bad)]) == 1
    assert "is not valid JSON" in capsys.readouterr().err
