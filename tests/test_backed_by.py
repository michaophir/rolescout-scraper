"""Which portfolio boards list a company, shown wherever a role is shown."""

import json

import pytest

from rolescout import candidates as cands
from rolescout import cli, companies, portfolio
from rolescout import shortlist as ranking
from tests.test_candidates import A, B, CONFIG, board_pages, co, run
from tests.test_shortlist import SMALL_CONFIG, cfg, day, mk, role_id, world  # noqa: F401  (world is a fixture)

RANKING = SMALL_CONFIG.replace("size = 3", "size = 20")


@pytest.fixture
def backed(conn, world, isolated_db_path, monkeypatch):
    """Alpha, added by hand, is listed by two boards; Beta by none; Newco was
    activated from a board. Any request after the harvest fails the test."""
    companies.set_website(conn, "Alpha", "https://www.alpha.example/about")
    world("Alpha", [mk("a1", "Senior Product Manager", "New York"), mk("a2", "Product Manager")], on_day=10)
    world("Beta", [mk("b1", "Product Manager")], on_day=10)
    portfolio.add_board(conn, A, "Alpha Fund")
    portfolio.add_board(conn, B)                                   # named by the board itself
    alpha = dict(domain="alpha.example", id=1)
    filters = cands.parse_config(CONFIG, "test")
    run(conn, filters, {
        **board_pages(A, "ignored", "1", [co("alpha", **alpha), co("newco", "newco.example", id=2)]),
        **board_pages(B, "Beta Capital", "2", [co("alpha", **alpha)])})
    cands.activate(conn, cands.get_candidate(conn, "newco.example"))
    (isolated_db_path.parent / "ranking.toml").write_text(RANKING)
    ranking.build_shortlist(conn, cfg(RANKING), now=day(10))

    def no_network(*args, **kwargs):
        raise AssertionError("showing who backs a company must not make a request")
    monkeypatch.setattr("requests.Session.request", no_network)
    return conn


def test_board_names_come_from_the_stored_listings(backed):
    ids = {r["name"]: r["id"] for r in backed.execute("SELECT id, name FROM companies")}
    assert companies.backed_by(backed) == {ids["Alpha"]: ["Alpha Fund", "Beta Capital"],
                                           ids["Newco"]: ["Alpha Fund"]}
    listed = {c["name"]: c["backed_by"] for c in companies.list_companies(backed)}
    assert listed == {"Alpha": ["Alpha Fund", "Beta Capital"], "Beta": [], "Gamma": [], "Newco": ["Alpha Fund"]}
    # Unregistering a board forgets its listings.
    portfolio.remove_board(backed, B)
    assert companies.backed_by(backed)[ids["Alpha"]] == ["Alpha Fund"]


def test_shortlist_has_a_backed_by_column(backed, capsys):
    assert cli.main(["shortlist"]) == 0
    lines = capsys.readouterr().out.splitlines()
    header = next(line for line in lines if line.startswith("Rank"))
    assert header.split() == ["Rank", "ID", "Score", "Company", "Tier", "Title", "Location",
                              "Backed", "by", "Flags", "URL"]
    start = header.index("Backed by")

    def cell(job):
        row = next(line for line in lines if line.endswith(f"example.com/{job}"))
        return row[start:start + 28].strip()
    assert cell("a1") == cell("a2") == "Alpha Fund, Beta Capital"
    assert cell("b1") == ""                                       # no listings: nothing shown

    assert cli.main(["shortlist", "--by-company"]) == 0
    out = capsys.readouterr().out
    headers = [line for line in out.splitlines() if line.startswith("Rank")]
    assert len(headers) == 2 and all("Backed by" in h for h in headers)
    assert out.count("Alpha Fund, Beta Capital") == 2               # Alpha's two roles

    assert cli.main(["shortlist", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)["entries"]
    assert {e["company"]: e["backed_by"] for e in entries} == {
        "Alpha": ["Alpha Fund", "Beta Capital"], "Beta": []}


def test_no_column_when_no_company_shown_is_listed_by_a_board(conn, world, capsys, isolated_db_path):
    world("Alpha", [mk("a1", "Product Manager")], on_day=10)
    (isolated_db_path.parent / "ranking.toml").write_text(RANKING)
    ranking.build_shortlist(conn, cfg(RANKING), now=day(10))
    assert cli.main(["shortlist"]) == 0
    assert "Backed by" not in capsys.readouterr().out
    assert cli.main(["companies", "list"]) == 0
    assert "Backed by" not in capsys.readouterr().out
    assert cli.main(["why", str(role_id(conn, "a1"))]) == 0
    assert "Backed by" not in capsys.readouterr().out


def test_why_and_companies_list_show_the_boards(backed, capsys):
    assert cli.main(["why", str(role_id(backed, "a1"))]) == 0
    assert "Backed by: Alpha Fund, Beta Capital" in capsys.readouterr().out
    assert cli.main(["why", str(role_id(backed, "b1"))]) == 0
    assert "Backed by" not in capsys.readouterr().out
    assert cli.main(["why", str(role_id(backed, "a1")), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["role"]["backed_by"] == ["Alpha Fund", "Beta Capital"]

    assert cli.main(["companies", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    start = lines[0].index("Backed by")
    shown = {line.split()[0]: line[start:].strip() for line in lines[2:] if line[:1].isalpha()}
    assert (shown["Alpha"], shown["Beta"], shown["Newco"]) == ("Alpha Fund, Beta Capital", "", "Alpha Fund")
    assert cli.main(["companies", "list", "--json"]) == 0
    assert {c["name"]: c["backed_by"] for c in json.loads(capsys.readouterr().out)}["Newco"] == ["Alpha Fund"]


def test_mcp_shortlist_and_role_carry_the_boards(backed, isolated_db_path):
    pytest.importorskip("mcp")
    from rolescout import mcp_server
    from tests.test_mcp import Session
    session = Session(mcp_server.build_server(isolated_db_path, read_only=True))
    flat = session.call("get_shortlist")["entries"]
    assert {e["company"]: e["backed_by"] for e in flat} == {"Alpha": ["Alpha Fund", "Beta Capital"], "Beta": []}
    grouped = session.call("get_shortlist", view="by_company")["companies"]
    assert {c["company"]: c["backed_by"] for c in grouped} == {
        "Alpha": ["Alpha Fund", "Beta Capital"], "Beta": []}
    assert "backed_by" not in grouped[0]["roles"][0]                # said once, on the company
    assert session.call("get_role", role_id=role_id(backed, "a1"))["backed_by"] == ["Alpha Fund", "Beta Capital"]
    assert session.call("get_role", role_id=role_id(backed, "b1"))["backed_by"] == []
