"""Company profiles, the fixed stage list, sector mapping, the VC list, and the browsing views."""

import json

import pytest

from rolescout import candidates as cands
from rolescout import cli, companies, db, portfolio, profiles, scout, vcs
from rolescout.db import transaction
from tests.test_candidates import A, B, CONFIG, board_pages, co, run
from tests.test_scout import Scripted
from tests.test_shortlist import mk

MAPPING = '''
[sectors]
"fintech" = ["FinTech", "Payments"]
"AI and ML" = ["Artificial Intelligence (AI)", "Machine Learning"]
"health tech" = ["Health Care"]
'''


def listed(slug, domain, **fields):
    """A company as a board lists it, with a description on its page."""
    description = fields.pop("description", f"{slug.title()} makes things.")
    company = co(slug, domain, **fields)
    company["description"] = description
    return company


@pytest.fixture
def filters():
    return cands.parse_config(CONFIG, "test")


@pytest.fixture
def harvested(conn, filters, isolated_db_path):
    """Acme was added by hand and is listed by two boards; Bolt is activated
    from one of them. The sector mapping file sits next to the database."""
    (isolated_db_path.parent / "sectors.toml").write_text(MAPPING)
    companies.add_company(conn, "Acme", website="https://www.acme.example")
    portfolio.add_board(conn, A, "Alpha Venture Partners")
    portfolio.add_board(conn, B)                                   # named by the board itself
    acme = dict(domain="acme.example", id=1, stage="series_f", employees=420,
                sectors=("FinTech", "Artificial Intelligence (AI)", "Quantum Widgets"),
                locations=("Austin, TX, USA", "London, UK"), description="Acme moves money for robots.")
    run(conn, filters, {
        **board_pages(A, "ignored", "1", [listed("acme", **acme),
                                          listed("bolt", "bolt.example", id=2, stage="seed",
                                                 sectors=("Health Care", "Quantum Widgets"))]),
        **board_pages(B, "Beta City Tech Network", "2", [listed("acme", **acme)])})
    cands.activate(conn, cands.get_candidate(conn, "bolt.example"))
    return conn


def company_id(conn, name):
    return conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()["id"]


# --- stage -----------------------------------------------------------------------

@pytest.mark.parametrize("raw, stage", [
    ("pre_seed", "pre-seed"), ("seed", "seed"), ("series_a", "series A"), ("series_b", "series B"),
    ("series_c", "series C"), ("series_d", "series D"), ("series_e", "series E+"), ("series_f", "series E+"),
    ("series_j", "series E+"), ("private_equity", "late-stage private"), ("ipo", "public"),
    ("acquisition", "acquired"), ("undisclosed", "unknown"), ("other", "unknown"),
    ("series_unknown", "unknown"), ("", "unknown"), (None, "unknown"), ("something new", "unknown"),
    ("Series A", "series A"), ("bootstrapped", "bootstrapped"),
])
def test_board_stages_map_onto_the_fixed_list(raw, stage):
    assert profiles.map_stage(raw) == stage and stage in profiles.STAGES


def test_the_stage_list_is_fixed_and_a_stage_typed_by_hand_must_be_on_it():
    assert profiles.STAGES == ("bootstrapped", "pre-seed", "seed", "series A", "series B", "series C",
                               "series D", "series E+", "late-stage private", "public", "acquired", "unknown")
    assert profiles.parse_stage("Series E and later") == "series E+"
    assert profiles.parse_stage("late-stage private") == "late-stage private"
    with pytest.raises(profiles.ProfileError, match="is not a stage; use one of: bootstrapped, pre-seed"):
        profiles.parse_stage("unicorn")


# --- backfill --------------------------------------------------------------------

def test_harvest_and_activation_fill_profiles_from_the_boards(harvested):
    acme = profiles.get_profile(harvested, "Acme")
    assert (acme["stage"]["value"], acme["stage"]["raw"], acme["stage"]["source"]) == ("series E+", "series_f", "board")
    assert (acme["employees"]["value"], acme["employees"]["source"]) == (420, "board")
    assert (acme["hq"]["value"], acme["hq"]["source"]) == ("Austin, TX, USA", "board")
    assert (acme["description"]["value"], acme["description"]["source"]) == ("Acme moves money for robots.", "board")
    assert acme["sectors"]["value"] == ["fintech", "AI and ML"] and acme["sectors"]["source"] == "board"
    # The raw tags are all kept, the one no sector claims included.
    assert acme["sectors"]["raw_tags"] == ["FinTech", "Artificial Intelligence (AI)", "Quantum Widgets"]
    assert all(acme[f]["set_at"] for f in profiles.FIELDS)
    # A company activated from a board gets its profile at once.
    bolt = profiles.get_profile(harvested, "Bolt")
    assert (bolt["stage"]["value"], bolt["sectors"]["value"], bolt["employees"]["value"]) == ("seed", ["health tech"], 50)
    assert bolt["description"]["value"] == "Bolt makes things."


def test_a_value_set_by_hand_survives_backfill(harvested, filters):
    profiles.set_field(harvested, "Acme", "stage", "public")
    profiles.set_field(harvested, "Acme", "sectors", "health tech, fintech")
    profiles.set_field(harvested, "Acme", "employees", "1,200")
    before = profiles.get_profile(harvested, "Acme")
    cands.apply_filter(harvested, filters)                          # a backfill, as after any harvest
    with transaction(harvested):
        profiles.backfill(harvested)
    after = profiles.get_profile(harvested, "Acme")
    assert after == before
    assert (after["stage"]["value"], after["stage"]["source"]) == ("public", "manual")
    assert after["sectors"]["value"] == ["fintech", "health tech"]   # in the list's own order
    assert (after["employees"]["value"], after["hq"]["source"]) == (1200, "board")
    # Clearing the hand-set value hands the field back to the board.
    assert profiles.clear_field(harvested, "Acme", "stage") is True
    cleared = profiles.get_profile(harvested, "Acme")["stage"]
    assert (cleared["value"], cleared["source"]) == ("series E+", "board")


def test_agent_values_sit_between_board_and_manual(harvested):
    profiles.set_field(harvested, "Acme", "description", "Payments for robots.", source="agent")
    with transaction(harvested):
        profiles.backfill(harvested)
    assert profiles.get_profile(harvested, "Acme")["description"] == {
        "value": "Payments for robots.", "source": "agent",
        "set_at": profiles.get_profile(harvested, "Acme")["description"]["set_at"]}
    profiles.set_field(harvested, "Acme", "description", "Robot payroll.")           # by hand
    with pytest.raises(profiles.ProfileError, match="description was set by hand and is not replaced by an agent"):
        profiles.set_field(harvested, "Acme", "description", "Something else.", source="agent")
    assert profiles.get_profile(harvested, "Acme")["description"]["value"] == "Robot payroll."
    with pytest.raises(profiles.ProfileError, match="is not a sector; use: fintech, AI and ML, health tech"):
        profiles.set_field(harvested, "Acme", "sectors", "fintech, widgets")
    with pytest.raises(profiles.ProfileError, match="employees must be a whole number"):
        profiles.set_field(harvested, "Acme", "employees", "many")


# --- sectors -----------------------------------------------------------------------

def test_unmapped_tags_are_kept_and_listed_with_counts(harvested, capsys, isolated_db_path):
    mapping = profiles.load_mapping(harvested)
    assert mapping.sectors == ("fintech", "AI and ML", "health tech") and mapping.from_file
    assert profiles.unmapped_tags(harvested, mapping) == [("Quantum Widgets", 2)]
    assert cli.main(["sectors", "unmapped"]) == 0
    out = capsys.readouterr().out
    assert "Quantum Widgets  2" in out and "1 unmapped tags" in out

    # Extending the file and applying it needs no network (the socket block proves it).
    (isolated_db_path.parent / "sectors.toml").write_text(
        MAPPING + '"industrial and logistics" = ["quantum  widgets"]\n')      # spelling does not matter
    assert cli.main(["sectors", "apply"]) == 0
    assert "Sector mapping applied (no network)" in capsys.readouterr().out
    assert profiles.get_profile(harvested, "Acme")["sectors"]["value"] == [
        "fintech", "AI and ML", "industrial and logistics"]
    assert profiles.get_profile(harvested, "Bolt")["sectors"]["value"] == ["health tech", "industrial and logistics"]
    assert profiles.unmapped_tags(harvested, profiles.load_mapping(harvested)) == []
    assert cli.main(["sectors", "list"]) == 0
    assert "industrial and logistics  2" in capsys.readouterr().out


def test_starter_mapping_is_written_from_the_tags_in_the_database(harvested, capsys, isolated_db_path):
    path = isolated_db_path.parent / "sectors.toml"
    assert cli.main(["sectors", "init"]) == 1                       # never overwrites
    assert "already exists" in capsys.readouterr().err
    path.unlink()
    built_in = profiles.load_mapping(harvested)
    assert not built_in.from_file and len(built_in.sectors) == 12
    assert {"health tech", "fintech", "AI and ML", "developer tools", "data and infrastructure", "security",
            "media and adtech", "commerce", "HR and work"} <= set(built_in.sectors)
    assert cli.main(["sectors", "init"]) == 0
    assert "3 of the 4 tags in your data placed in 12 sectors, 1 left unmapped" in capsys.readouterr().out
    text = path.read_text()
    assert '"fintech" = [\n    "FinTech",\n]' in text and '"health tech" = [\n    "Health Care",\n]' in text
    assert '#    "Quantum Widgets",' in text                          # listed, for the owner to place
    written = profiles.load_mapping(harvested)
    assert written.from_file and written.apply(["FinTech", "Artificial Intelligence (AI)", "Quantum Widgets"]) == [
        "fintech", "AI and ML"]
    with pytest.raises(profiles.MappingError, match="must have one \\[sectors\\] table"):
        profiles.parse_mapping('fintech = ["Payments"]\n')


# --- the VC list -------------------------------------------------------------------

def test_a_vc_is_made_from_each_board_and_links_follow_the_listings(harvested):
    rows = {v["name"]: v for v in vcs.list_vcs(harvested)}
    assert set(rows) == {"Alpha Venture Partners", "Beta City Tech Network"}
    alpha, beta = rows["Alpha Venture Partners"], rows["Beta City Tech Network"]
    assert (alpha["short_name"], alpha["type"], alpha["boards"], alpha["companies"]) == ("Alpha", "vc", [A], 2)
    assert (alpha["website"], beta["website"]) == ("https://alpha-fund.example", "https://beta-capital.example")
    # Named from the host until the harvest learned the board's own name.
    assert (beta["short_name"], beta["boards"], beta["companies"]) == ("Beta City Tech", [B], 1)
    links = {(r["company"], r["vc"], r["source"]) for r in harvested.execute(
        "SELECT c.name AS company, v.short_name AS vc, l.source FROM company_vcs l "
        "JOIN companies c ON c.id = l.company_id JOIN vcs v ON v.id = l.vc_id")}
    assert links == {("Acme", "Alpha", "board"), ("Acme", "Beta City Tech", "board"), ("Bolt", "Alpha", "board")}
    assert companies.backed_by(harvested) == {
        company_id(harvested, "Acme"): ["Alpha", "Beta City Tech"], company_id(harvested, "Bolt"): ["Alpha"]}


def test_an_ecosystem_group_is_left_out_of_backed_by(harvested, capsys):
    assert cli.main(["vcs", "edit", "Beta City Tech Network", "--type", "ecosystem", "--short", "Beta City"]) == 0
    assert "Beta City Tech Network: shown as Beta City, type ecosystem" in capsys.readouterr().out
    acme = company_id(harvested, "Acme")
    assert companies.backed_by(harvested)[acme] == ["Alpha"]
    assert next(c for c in companies.list_companies(harvested) if c["name"] == "Acme")["backed_by"] == ["Alpha"]
    row = harvested.execute("SELECT backed_by, ecosystems FROM v_company_overview WHERE company = 'Acme'").fetchone()
    assert (row["backed_by"], row["ecosystems"]) == ("Alpha", "Beta City")
    # The link itself is kept: it is the display that leaves the group out.
    assert harvested.execute("SELECT COUNT(*) FROM company_vcs WHERE company_id = ?", (acme,)).fetchone()[0] == 2
    assert cli.main(["profile", "show", "Acme"]) == 0
    out = capsys.readouterr().out
    assert "Backed by: Alpha Venture Partners (board)" in out
    assert "Ecosystem groups: Beta City Tech Network (board)" in out


def test_a_vc_link_made_by_hand(harvested, filters, capsys):
    companies.add_company(conn := harvested, "Cobalt")
    assert cli.main(["vcs", "add", "Gamma Capital", "--website", "gamma.example"]) == 0
    assert "Added Gamma Capital (shown as Gamma, type vc)." in capsys.readouterr().out
    assert cli.main(["vcs", "link", "Cobalt", "gamma"]) == 0                       # by short name
    assert "Cobalt linked to Gamma Capital by hand." in capsys.readouterr().out
    cobalt = company_id(conn, "Cobalt")
    assert companies.backed_by(conn)[cobalt] == ["Gamma"]
    assert conn.execute("SELECT source FROM company_vcs WHERE company_id = ?", (cobalt,)).fetchone()["source"] == "manual"
    cands.apply_filter(conn, filters)                               # a backfill leaves hand-made links alone
    assert companies.backed_by(conn)[cobalt] == ["Gamma"]
    # By hand on top of a board link: it stays when the board goes.
    assert cli.main(["vcs", "link", "Bolt", "Alpha Venture Partners"]) == 0
    portfolio.remove_board(conn, A)
    assert companies.backed_by(conn) == {company_id(conn, "Acme"): ["Beta City Tech"],
                                         company_id(conn, "Bolt"): ["Alpha"], cobalt: ["Gamma"]}
    assert cli.main(["vcs", "unlink", "Cobalt", "Gamma Capital"]) == 0
    assert "Cobalt is no longer linked to Gamma Capital." in capsys.readouterr().out.splitlines()[-1]
    assert cobalt not in companies.backed_by(conn)
    assert cli.main(["vcs", "link", "Nobody", "Gamma Capital"]) == 1
    assert "no company named" in capsys.readouterr().err


def test_vc_commands_add_list_edit_attach_remove(harvested, capsys):
    assert cli.main(["vcs", "add", "Alpha Venture Partners"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert cli.main(["vcs", "add", "Delta Ventures", "--short", "DV", "--type", "ecosystem"]) == 0
    capsys.readouterr()
    assert cli.main(["vcs", "list"]) == 0
    out = capsys.readouterr().out
    assert "Alpha Venture Partners  Alpha" in out and "Delta Ventures" in out and "ecosystem" in out
    assert cli.main(["vcs", "list", "--json"]) == 0
    assert [v["name"] for v in json.loads(capsys.readouterr().out)] == [
        "Delta Ventures", "Alpha Venture Partners", "Beta City Tech Network"]       # ecosystems first, then by name

    # The second board belongs to the first firm after all.
    assert cli.main(["vcs", "attach", B, "Alpha"]) == 0
    assert f"{B} now belongs to Alpha Venture Partners." in capsys.readouterr().out
    rows = {v["name"]: v for v in vcs.list_vcs(harvested)}
    assert rows["Alpha Venture Partners"]["boards"] == [A, B] and rows["Beta City Tech Network"]["companies"] == 0
    assert companies.backed_by(harvested)[company_id(harvested, "Acme")] == ["Alpha"]

    assert cli.main(["vcs", "remove", "Alpha Venture Partners"]) == 0
    assert "Removed Alpha Venture Partners: 2 company links removed, 2 boards left registered with no VC." in (
        capsys.readouterr().out)
    assert companies.backed_by(harvested) == {}
    assert len(portfolio.list_boards(harvested)) == 2               # the boards and their candidates stay
    assert cli.main(["vcs", "edit", "Delta Ventures"]) == 1
    assert cli.main(["vcs", "remove", "Nobody"]) == 1
    assert "no VC named 'Nobody'" in capsys.readouterr().err
    # A new board can be registered straight under an existing VC.
    assert cli.main(["portfolio", "add", "https://talent.delta.example", "--vc", "DV"]) == 0
    assert "Registered https://talent.delta.example under Delta Ventures" in capsys.readouterr().out
    assert vcs.short_name("Acme Capital Partners") == "Acme" and vcs.short_name("Capital") == "Capital"


# --- upgrade -----------------------------------------------------------------------

def test_upgrade_makes_vcs_from_boards_and_backfills_without_a_network(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    current = db.SCHEMA_VERSION
    monkeypatch.setattr(db, "SCHEMA_VERSION", 8)
    old = db.connect(path)
    assert db.schema_version(old) == 8
    with transaction(old):
        old.execute("INSERT INTO companies (name, website, tier, created_at) VALUES ('Acme', 'https://acme.example', 1, 'x')")
        old.execute("INSERT INTO boards (company_id, source, ref, mapped_at) VALUES (1, 'greenhouse', 'acme', 'x')")
        old.execute("INSERT INTO roles (source, source_job_id, company_id, board_id, title, first_seen_at, last_seen_at) "
                    "VALUES ('greenhouse', 'a1', 1, 1, 'Product Manager', '2026-03-10T08:00:00Z', '2026-03-10T08:00:00Z')")
        old.execute("INSERT INTO portfolio_boards (url, name, added_at) VALUES "
                    "('https://jobs.alpha.example', 'Alpha Ventures', 'x'), ('https://careers.city.example', '', 'x')")
        old.execute("INSERT INTO candidates (domain, name, stage, employees, sectors, locations, company_id, first_seen_at) "
                    "VALUES ('acme.example', 'Acme Inc', 'series_b', 80, '[\"Payments\", \"Odd Tag\"]', '[\"Denver, CO\"]', 1, 'x')")
        old.execute("INSERT INTO candidate_listings (candidate_id, board_id, slug, listed_at, last_listed_at) "
                    "VALUES (1, 1, 'acme', '2026-02-01T00:00:00Z', 'x'), (1, 2, 'acme', '2026-02-02T00:00:00Z', 'x')")
    roles_before = [tuple(r) for r in old.execute("SELECT * FROM roles")]
    old.close()
    monkeypatch.setattr(db, "SCHEMA_VERSION", current)

    conn = db.connect(path)                                        # sockets are blocked: no network here
    assert db.schema_version(conn) == current >= 9
    assert [tuple(r) for r in conn.execute("SELECT * FROM roles")] == roles_before      # history untouched
    made = [(v["name"], v["short_name"], v["type"], v["website"], v["boards"]) for v in vcs.list_vcs(conn)]
    assert made == [("Alpha Ventures", "Alpha", "vc", "https://alpha.example", ["https://jobs.alpha.example"]),
                    ("careers.city.example", "careers.city.example", "vc", "https://city.example",
                     ["https://careers.city.example"])]
    assert [(r["source"], r["linked_at"]) for r in conn.execute("SELECT * FROM company_vcs ORDER BY vc_id")] == [
        ("board", "2026-02-01T00:00:00Z"), ("board", "2026-02-02T00:00:00Z")]
    acme = profiles.get_profile(conn, "Acme")
    assert (acme["stage"]["value"], acme["stage"]["raw"], acme["employees"]["value"], acme["hq"]["value"]) == (
        "series B", "series_b", 80, "Denver, CO")
    assert acme["sectors"]["value"] == ["fintech"] and acme["sectors"]["raw_tags"] == ["Payments", "Odd Tag"]
    assert acme["description"]["value"] is None                    # the old harvest stored none
    assert conn.execute("SELECT tier FROM companies").fetchone()["tier"] == 1           # the column stays


# --- views -------------------------------------------------------------------------

@pytest.fixture
def scouted(harvested):
    """Acme fetched on three days in two weeks: one role opens, one closes."""
    companies.add_board(harvested, "Acme", "greenhouse", "acme")
    a1, a2, a3 = mk("a1", "Product Manager", "Austin, TX"), mk("a2", "Designer"), mk("a3", "Engineer", "Remote")
    script = Scripted(acme=[[a1, a2], [a1, a3], [a1, a3]])
    for stamp in ("2026-03-02T08:00:00Z", "2026-03-04T08:00:00Z", "2026-03-10T08:00:00Z"):
        scout.run_scout(harvested, adapter_for=script, clock=lambda: stamp)
    return harvested


def test_view_open_roles_with_profile_and_vcs(scouted):
    rows = [dict(r) for r in scouted.execute("SELECT * FROM v_open_roles ORDER BY title")]
    assert [(r["company"], r["title"], r["location"]) for r in rows] == [
        ("Acme", "Engineer", "Remote"), ("Acme", "Product Manager", "Austin, TX")]      # the closed role is absent
    first = rows[0]
    assert (first["stage"], first["employees"], first["hq"]) == ("series E+", 420, "Austin, TX, USA")
    assert json.loads(first["sectors"]) == ["fintech", "AI and ML"]
    assert first["backed_by"] == "Alpha, Beta City Tech" and first["url"] == "https://example.com/a3"
    companies.set_active(scouted, "Acme", False)
    assert scouted.execute("SELECT COUNT(*) FROM v_open_roles").fetchone()[0] == 0


def test_view_company_overview(scouted):
    acme = dict(scouted.execute("SELECT * FROM v_company_overview WHERE company = 'Acme'").fetchone())
    assert (acme["stage"], acme["employees"], acme["hq"], acme["description"]) == (
        "series E+", 420, "Austin, TX, USA", "Acme moves money for robots.")
    assert (acme["backed_by"], acme["ecosystems"], acme["job_boards"]) == ("Alpha, Beta City Tech", None, "greenhouse/acme")
    assert (acme["open_roles"], acme["opened_30d"], acme["closed_30d"]) == (2, 0, 0)    # the scouts were long ago
    # A scout today: what it opens and closes counts toward the last 30 days.
    script = Scripted(acme=[[mk("a1", "Product Manager"), mk("a3", "Engineer"), mk("a4", "Analyst")],
                            [mk("a4", "Analyst")], [mk("a4", "Analyst")]])
    for _ in range(3):
        scout.run_scout(scouted, adapter_for=script)
    acme = dict(scouted.execute("SELECT * FROM v_company_overview WHERE company = 'Acme'").fetchone())
    assert (acme["open_roles"], acme["opened_30d"], acme["closed_30d"]) == (1, 1, 2)
    bolt = dict(scouted.execute("SELECT * FROM v_company_overview WHERE company = 'Bolt'").fetchone())
    assert (bolt["stage"], bolt["backed_by"], bolt["open_roles"], bolt["job_boards"]) == ("seed", "Alpha", 0, None)
    cobalt_free = scouted.execute("SELECT COUNT(*) FROM v_company_overview").fetchone()[0]
    assert cobalt_free == 2                                        # one row per company, profile or not


def test_view_hiring_per_company_per_week(scouted):
    rows = [dict(r) for r in scouted.execute("SELECT * FROM v_hiring_weekly ORDER BY week_start")]
    assert [(r["company"], r["week_start"], r["opened"], r["closed"], r["net"], r["open_roles_at_week_end"], r["scouts"])
            for r in rows] == [
        ("Acme", "2026-03-02", 1, 0, 1, 3, 2),       # a3 opens (a2, missed once, still counts as open)
        ("Acme", "2026-03-09", 0, 1, -1, 2, 1)]      # a2 closes on its second miss


def test_view_company_sectors_gives_one_row_per_sector(harvested):
    rows = [(r["company"], r["sector"], r["stage"]) for r in harvested.execute(
        "SELECT * FROM v_company_sectors ORDER BY company, sector")]
    assert rows == [("Acme", "AI and ML", "series E+"), ("Acme", "fintech", "series E+"), ("Bolt", "health tech", "seed")]


# --- commands and MCP --------------------------------------------------------------

def test_profile_commands_show_set_clear_coverage(harvested, capsys):
    assert cli.main(["profile", "show", "acme"]) == 0
    out = capsys.readouterr().out
    assert "stage        series E+  (given as series_f)" in out and "board" in out
    assert "Board tags (3): FinTech, Artificial Intelligence (AI), Quantum Widgets" in out
    assert cli.main(["profile", "set", "Acme", "hq", "Denver,", "CO"]) == 0
    assert "Acme: hq set to Denver, CO. Board and agent data will not replace it." in capsys.readouterr().out
    assert cli.main(["profile", "set", "Acme", "stage", "unicorn"]) == 1
    assert "is not a stage" in capsys.readouterr().err
    assert cli.main(["profile", "show", "Acme", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["hq"]["source"] == "manual"
    assert cli.main(["profile", "clear", "Acme", "hq"]) == 0
    assert "Acme: hq cleared; the board's value applies again: Austin, TX, USA" in capsys.readouterr().out
    companies.add_company(harvested, "Cobalt")
    assert cli.main(["profile", "coverage"]) == 0
    out = capsys.readouterr().out
    assert "Profiles of 3 active companies:" in out
    assert "stage (not unknown):       2  67%" in out and "description:               2  67%" in out
    assert cli.main(["profile", "show", "Nobody"]) == 1


def test_mcp_profile_and_vc_tools(harvested, isolated_db_path):
    pytest.importorskip("mcp")
    from rolescout import mcp_server
    from tests.test_mcp import Session
    session = Session(mcp_server.build_server(isolated_db_path))
    profile = session.call("get_company_profile", company="acme")
    assert profile["stage"]["value"] == "series E+" and profile["sector_list"] == ["fintech", "AI and ML", "health tech"]
    assert profile["backed_by"] == [{"vc": "Alpha Venture Partners", "source": "board"},
                                    {"vc": "Beta City Tech Network", "source": "board"}]
    set_ = session.call("set_company_profile", company="Acme", field="description", value="Robot payments.")
    assert set_["description"] == {"value": "Robot payments.", "source": "agent", "set_at": set_["description"]["set_at"]}
    profiles.set_field(harvested, "Acme", "hq", "Denver, CO")
    assert "was set by hand" in session.error("set_company_profile", company="Acme", field="hq", value="Paris")

    assert [v["name"] for v in session.call("list_vcs")["vcs"]] == ["Alpha Venture Partners", "Beta City Tech Network"]
    assert session.call("add_vc", name="Gamma Capital") == {
        "name": "Gamma Capital", "short_name": "Gamma", "type": "vc", "website": None}
    assert session.call("edit_vc", vc="Beta City Tech Network", type="ecosystem")["type"] == "ecosystem"
    assert session.call("get_company_profile", company="Acme")["ecosystem_groups"] == ["Beta City Tech Network"]
    assert session.call("link_company_to_vc", company="Bolt", vc="Gamma") == {
        "company": "Bolt", "vc": "Gamma Capital", "newly_linked": True}
    assert companies.backed_by(harvested)[company_id(harvested, "Bolt")] == ["Alpha", "Gamma"]
    assert session.call("unlink_company_from_vc", company="Bolt", vc="Alpha") == {
        "company": "Bolt", "vc": "Alpha Venture Partners", "removed": True, "still_linked": True}
    assert session.call("attach_board_to_vc", board=B, vc="Gamma Capital") == {"board": B, "vc": "Gamma Capital"}
    assert session.call("remove_vc", vc="Gamma Capital") == {
        "removed": "Gamma Capital", "company_links_removed": 2, "boards_left_without_a_vc": 1}
    assert "no VC named" in session.error("edit_vc", vc="Nobody", type="vc")

    read_only = {t.name for t in Session(mcp_server.build_server(isolated_db_path, read_only=True)).tools()}
    assert {"get_company_profile", "list_vcs"} <= read_only
    assert not {"set_company_profile", "add_vc", "edit_vc", "remove_vc", "attach_board_to_vc",
                "link_company_to_vc", "unlink_company_from_vc"} & read_only
