"""The starter set and ``rolescout init``: what a new install begins with."""

from collections import Counter

from rolescout import candidates as cands, cli, companies, db, portfolio, starter
from rolescout import shortlist as ranking
from rolescout.adapters import ADAPTERS
from rolescout.resolve import registrable_domain
from rolescout.transfer import FORMAT, FORMAT_VERSION
from rolescout.urls import normalize_website


def test_starter_companies_are_a_loadable_export_with_one_confirmed_board_each():
    data = starter.companies()
    assert (data["format"], data["version"]) == (FORMAT, FORMAT_VERSION)
    listed = data["companies"]
    assert 100 <= len(listed) <= 150
    names = [c["name"] for c in listed]
    assert len({n.casefold() for n in names}) == len(names) and names == sorted(names, key=str.casefold)
    boards = set()
    for company in listed:
        assert normalize_website(company["website"]) == company["website"], company["name"]
        assert registrable_domain(company["website"]), company["name"]
        assert len(company["boards"]) == 1, company["name"]
        board = company["boards"][0]
        adapter = ADAPTERS[board["source"]]
        assert adapter.canonical_ref(board["ref"]) == board["ref"], company["name"]     # the stored spelling
        assert board["origin"] == "starter" and board["evidence"].startswith("confirmed live "), company["name"]
        boards.add((board["source"], board["ref"]))
        # Nothing personal travels with the starter set.
        assert set(company) == {"name", "website", "tags", "active", "boards"} and company["tags"] == []
    assert len(boards) == len(listed)                       # no board given to two companies
    assert not data.get("vcs")
    by_source = Counter(source for source, _ in boards)
    assert by_source["greenhouse"] >= 20 and by_source["ashby"] >= 20 and len(by_source) >= 6


def test_example_portfolio_boards_are_board_urls():
    boards = starter.portfolio_boards()
    assert 3 <= len(boards) <= 8
    for board in boards:
        assert portfolio.board_base(board["url"]) == board["url"] and board["name"]


def test_init_sets_up_companies_boards_and_configs_in_one_command(isolated_db_path, capsys):
    expected = len(starter.companies()["companies"])
    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert f"Companies: {expected} starter companies added, 0 already here, {expected} boards mapped" in out
    assert "rolescout scout" in out and "Portfolio boards" not in out
    conn = db.connect()
    listed = companies.list_companies(conn)
    assert len(listed) == expected and all(c["active"] and len(c["boards"]) == 1 for c in listed)
    assert conn.execute("SELECT COUNT(*) FROM portfolio_boards").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM boards WHERE last_success_at IS NOT NULL").fetchone()[0] == 0

    # Both configs load, sit next to the database and are private.
    ranking_path, filter_path = isolated_db_path.parent / "ranking.toml", isolated_db_path.parent / "candidates.toml"
    assert list(ranking.load_config(ranking_path).wanted) == ranking.STARTER_TITLES
    assert cands.load_config(filter_path).sector_keywords == cands.STARTER_KEYWORDS
    assert ranking_path.stat().st_mode & 0o777 == filter_path.stat().st_mode & 0o777 == 0o600


def test_init_again_keeps_what_is_there(isolated_db_path, capsys):
    assert cli.main(["init"]) == 0
    expected = len(starter.companies()["companies"])
    conn = db.connect()
    first = companies.list_companies(conn)[0]["name"]
    companies.set_active(conn, first, False)
    companies.add_company(conn, "Acme", website="acme.com")
    ranking_path = isolated_db_path.parent / "ranking.toml"
    ranking_path.write_text(ranking_path.read_text() + "\n# my edit\n")
    capsys.readouterr()

    assert cli.main(["init", "--portfolio"]) == 0
    out = capsys.readouterr().out
    assert f"Companies: 0 starter companies added, {expected} already here, 0 boards mapped" in out
    assert f"Kept {ranking_path}" in out and "# my edit" in ranking_path.read_text()
    after = {c["name"]: c for c in companies.list_companies(conn)}
    assert len(after) == expected + 1 and after[first]["active"] is False     # a change made since is kept
    examples = len(starter.portfolio_boards())
    assert f"Portfolio boards: {examples} example boards registered" in out
    assert conn.execute("SELECT COUNT(*) FROM portfolio_boards").fetchone()[0] == examples
    assert cli.main(["init", "--portfolio"]) == 0                      # registering twice adds nothing
    assert "Portfolio boards: 0 example boards registered" in capsys.readouterr().out

    assert cli.main(["init", "--force"]) == 0
    assert "# my edit" not in ranking_path.read_text()


def test_init_without_companies_writes_only_the_configs(isolated_db_path, capsys):
    assert cli.main(["init", "--no-companies"]) == 0
    assert "Companies: skipped" in capsys.readouterr().out
    assert companies.list_companies(db.connect()) == []
    assert (isolated_db_path.parent / "ranking.toml").exists()
