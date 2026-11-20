"""Filtered ranking, the company overview, enrichment, leveling, no tier
anywhere, and an export that carries what was entered by hand."""

import json
import re

import pytest

from rolescout import cli, companies, db, filters, overview, portfolio, profiles, scout, vcs
from rolescout import shortlist as ranking
from rolescout.db import transaction
from rolescout.query import query_roles
from rolescout.transfer import export_companies, import_companies
from tests.test_scout import Scripted
from tests.test_shortlist import SMALL_CONFIG, cfg, day, mk

RANKING = SMALL_CONFIG.replace("size = 3", "size = 2\nmax_per_company = 0")      # a short list: two roles
MAPPING = '[sectors]\n"fintech" = ["Payments"]\n"health tech" = ["Health Care"]\n"AI and ML" = ["AI"]\n'


def profile(conn, name, **fields):
    for field, value in fields.items():
        profiles.set_field(conn, name, field, value, source="agent")


@pytest.fixture
def world(conn, isolated_db_path):
    """Four companies with profiles, roles and VCs, and today's two-role shortlist."""
    (isolated_db_path.parent / "ranking.toml").write_text(RANKING)
    (isolated_db_path.parent / "sectors.toml").write_text(MAPPING)
    roles = {
        "Alpha": [mk("a1", "Senior Product Manager", "New York, NY"), mk("a2", "Staff Product Manager", "Remote")],
        "Beta": [mk("b1", "Product Manager", "London, UK"), mk("b2", "Product Manager", "Austin, TX | London")],
        "Gamma": [mk("g1", "Product Manager", "Berlin")],
        "Delta": [mk("d1", "Product Manager", "Remote - Canada")],
    }
    for name in roles:
        companies.add_company(conn, name, website=f"https://{name.lower()}.example")
        companies.add_board(conn, name, "greenhouse", name.lower())
    profile(conn, "Alpha", stage="series B", sectors="fintech", employees="400", hq="New York, NY")
    profile(conn, "Beta", stage="seed", sectors="health tech, AI and ML", employees="30", hq="London, UK")
    profile(conn, "Gamma", stage="seed", sectors="fintech", employees="12", hq="Berlin, Germany")
    # Delta has no profile at all.
    vcs.add_vc(conn, "Orbit Ventures")
    vcs.add_vc(conn, "City Tech Alliance", kind="ecosystem")
    vcs.link_company(conn, "Beta", "Orbit")
    vcs.link_company(conn, "Gamma", "Orbit")
    vcs.link_company(conn, "Delta", "City Tech Alliance")
    scout.run_scout(conn, adapter_for=Scripted(**{name.lower(): [posted] for name, posted in roles.items()}),
                    clock=lambda: "2026-03-10T08:00:00Z")
    ranking.build_shortlist(conn, ranking.parse_config(RANKING, "test"), now=day(10))
    return conn


def ranked(conn, **wanted):
    only = filters.build_filter(conn, **wanted)
    found = ranking.filtered_entries(conn, ranking.parse_config(RANKING, "test"), only, now=day(10))
    return [(e["company"], e["title"]) for e in found["entries"]], found


# --- each filter -------------------------------------------------------------------

def test_todays_list_is_the_top_two_only(world):
    listed = [(e["company"], e["title"]) for e in ranking.shortlist_entries(world, ranking.latest_shortlist(world)["id"])]
    assert listed == [("Alpha", "Senior Product Manager"), ("Alpha", "Staff Product Manager")]   # both score 50


def test_filter_by_stage(world):
    assert ranked(world, stage="seed")[0] == [("Beta", "Product Manager"), ("Beta", "Product Manager"),
                                              ("Gamma", "Product Manager")]
    assert len(ranked(world, stage=["seed", "Series B"])[0]) == 5          # any of several; spelling is loose
    assert ranked(world, stage="public")[0] == []
    with pytest.raises(filters.FilterError, match="is not a stage"):
        filters.build_filter(world, stage="unicorn")


def test_filter_by_sector(world):
    assert {c for c, _ in ranked(world, sector="fintech")[0]} == {"Alpha", "Gamma"}
    assert {c for c, _ in ranked(world, sector="AI and ML, health tech")[0]} == {"Beta"}
    with pytest.raises(filters.FilterError, match="is not a sector"):
        filters.build_filter(world, sector="widgets")


def test_filter_by_hq(world):
    assert {c for c, _ in ranked(world, hq="london")[0]} == {"Beta"}
    assert {c for c, _ in ranked(world, hq="Germany")[0]} == {"Gamma"}


def test_filter_by_role_location(world):
    us, _ = ranked(world, location="United States")
    assert us == [("Alpha", "Senior Product Manager"), ("Beta", "Product Manager")]     # NY; and Austin, TX | London
    assert {c for c, _ in ranked(world, location="London")[0]} == {"Beta"}
    remote, _ = ranked(world, location="remote")
    assert remote == [("Alpha", "Staff Product Manager"), ("Delta", "Product Manager")]
    # A place wants the place named: a bare "Remote" is not in Canada.
    assert ranked(world, location="Canada")[0] == [("Delta", "Product Manager")]


def test_filter_by_employee_range(world):
    assert {c for c, _ in ranked(world, employees="20-500")[0]} == {"Alpha", "Beta"}
    assert {c for c, _ in ranked(world, employees="-30")[0]} == {"Beta", "Gamma"}
    assert {c for c, _ in ranked(world, employees="100-")[0]} == {"Alpha"}
    assert {c for c, _ in ranked(world, employees_min=10, employees_max=15)[0]} == {"Gamma"}   # no size: left out
    for bad in ("many", "500-20", "20"):
        with pytest.raises(filters.FilterError):
            filters.build_filter(world, employees=bad)


def test_filter_by_vc_and_filters_combined(world):
    assert {c for c, _ in ranked(world, vc="Orbit")[0]} == {"Beta", "Gamma"}
    assert {c for c, _ in ranked(world, vc="City Tech Alliance")[0]} == {"Delta"}       # an ecosystem group works too
    assert ranked(world, vc="Orbit Ventures", stage="seed", sector="fintech")[0] == [("Gamma", "Product Manager")]
    with pytest.raises(filters.FilterError, match="no VC named"):
        filters.build_filter(world, vc="Nobody")
    only = filters.build_filter(world, stage="seed", sector="fintech", hq="Berlin", location="Berlin",
                                employees="10-20", vc="Orbit")
    assert only.describe() == ("stage seed; sector fintech; HQ in Berlin; role in Berlin; "
                               "10 to 20 employees; linked to Orbit Ventures")
    assert not filters.build_filter(world)


def test_a_filter_ranks_roles_that_are_outside_todays_list(world, capsys):
    names, found = ranked(world, stage="seed")
    assert found["qualifying"] == 6 and len(names) == 3
    # None of them is on today's two-role list; here they are ranked 1 to 3.
    assert [(e["rank"], e["list_rank"]) for e in found["entries"]] == [(1, None), (2, None), (3, None)]
    on_list = ranked(world, sector="fintech")[1]["entries"]
    assert [(e["company"], e["rank"], e["list_rank"]) for e in on_list] == [
        ("Alpha", 1, 1), ("Alpha", 2, 2), ("Gamma", 3, None)]

    assert cli.main(["shortlist", "--stage", "seed", "--vc", "Orbit"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Filtered ranking (stage seed; linked to Orbit Ventures): 3 of the 6 roles that qualify, "
                          "ranked now; 3 of them are not on today's shortlist")
    header = next(line for line in out.splitlines() if line.startswith("Rank"))
    assert header.split() == ["Rank", "ID", "Score", "Company", "Title", "Location", "Backed", "by", "On", "list",
                              "Flags", "URL"]
    assert len([l for l in out.splitlines() if l[:1].isdigit()]) == 3 and "Orbit" in out
    assert cli.main(["shortlist", "--sector", "fintech", "--top", "1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert (data["matching"], len(data["entries"]), data["entries"][0]["company"]) == (3, 1, "Alpha")
    assert cli.main(["shortlist", "--location", "United States", "--by-company"]) == 0
    assert "Beta: 1 role, best rank 2" in capsys.readouterr().out
    assert cli.main(["shortlist", "--stage", "unicorn"]) == 1
    assert "is not a stage" in capsys.readouterr().err


def test_the_same_filters_narrow_a_role_search(world, capsys):
    only = filters.build_filter(world, stage="seed", location="London")
    assert [(r["company"], r["title"], r["location"]) for r in query_roles(world, only=only)] == [
        ("Beta", "Product Manager", "London, UK"), ("Beta", "Product Manager", "Austin, TX | London")]
    assert len(query_roles(world, only=only, limit=1, offset=1)) == 1
    assert cli.main(["query", "--vc", "Orbit", "--employees", "-20"]) == 0
    out = capsys.readouterr().out
    assert "Gamma" in out and "Beta" not in out and "1 role." in out


# --- company overview --------------------------------------------------------------

def test_company_overview(world, capsys):
    script = Scripted(alpha=[[mk("a1", "Senior Product Manager", "New York, NY"), mk("a3", "Designer")]] * 2)
    for _ in range(2):                                             # today: a3 opens, a2 closes on its second miss
        scout.run_scout(world, adapter_for=script)
    data = overview.company_overview(world, "alpha", ranking.parse_config(RANKING, "test"), top=5)
    assert (data["company"], data["website"], data["job_boards"]) == ("Alpha", "https://alpha.example", ["greenhouse/alpha"])
    assert data["profile"]["stage"]["value"] == "series B" and data["profile"]["sectors"]["value"] == ["fintech"]
    assert (data["backed_by"], data["ecosystem_groups"], data["open_roles"]) == ([], [], 2)
    assert [(r["title"], r["qualifies"]) for r in data["top_roles"]] == [
        ("Senior Product Manager", True), ("Designer", False)]       # ranked; the ones that qualify first
    trend = data["hiring_30_days"]
    assert (trend["opened"], trend["closed"], trend["net"]) == (1, 1, 0) and len(trend["weeks"]) == 1
    assert overview.company_overview(world, "Beta", None, top=1)["backed_by"] == ["Orbit Ventures"]
    assert len(overview.company_overview(world, "Beta", None, top=1)["top_roles"]) == 1

    assert cli.main(["company", "Alpha", "--top", "1"]) == 0
    out = capsys.readouterr().out
    assert "stage        series B" in out and "Last 30 days: 1 opened, 1 closed; 2 open now" in out
    assert "Top 1 of 2 open roles" in out and "Senior Product Manager" in out and "Designer" not in out
    assert cli.main(["company", "Delta", "--json"]) == 0
    delta = json.loads(capsys.readouterr().out)
    assert delta["ecosystem_groups"] == ["City Tech Alliance"] and delta["profile"]["stage"]["value"] is None
    assert cli.main(["company", "Nobody"]) == 1


# --- enrichment --------------------------------------------------------------------

def test_missing_fields_are_listed_and_an_agent_fills_them_without_touching_manual_values(world, capsys):
    rows = {r["company"]: r["missing"] for r in profiles.missing(world)}
    assert rows == {"Alpha": ["description"], "Beta": ["description"], "Gamma": ["description"],
                    "Delta": ["description", "sectors", "stage", "employees", "hq"]}
    assert [r["company"] for r in profiles.missing(world, ["stage", "hq"])] == ["Delta"]
    assert profiles.missing(world)[0]["company"] in ("Alpha", "Beta")         # most open roles first
    assert cli.main(["profile", "missing", "--field", "stage"]) == 0
    out = capsys.readouterr().out
    assert "Delta" in out and "Alpha" not in out and "1 companies with a field missing" in out

    profiles.set_field(world, "Delta", "stage", "public")                     # the owner knows this one
    result = profiles.set_fields(world, "Delta", {
        "description": "Delta makes maps.", "stage": "seed", "employees": "lots", "hq": "Toronto, Canada",
        "sectors": "fintech", "website": None})
    assert result["set"] == ["description", "hq", "sectors"]
    assert result["kept"] == {"stage": "set by hand; not replaced"}
    assert "employees must be a whole number" in result["refused"]["employees"]
    delta = result["profile"]
    assert (delta["stage"]["value"], delta["stage"]["source"]) == ("public", "manual")
    assert (delta["hq"]["value"], delta["hq"]["source"]) == ("Toronto, Canada", "agent")
    assert {r["company"]: r["missing"] for r in profiles.missing(world)}["Delta"] == ["employees"]


def test_mcp_filters_overview_and_enrichment(world, isolated_db_path):
    pytest.importorskip("mcp")
    from rolescout import mcp_server
    from tests.test_mcp import Session
    session = Session(mcp_server.build_server(isolated_db_path))
    plain = session.call("get_shortlist")
    assert plain["roles_on_list"] == 2 and "filter" not in plain
    seed = session.call("get_shortlist", stage=["seed"], vc="Orbit")
    assert (seed["filter"], seed["roles_matching"], seed["not_on_todays_shortlist"]) == (
        "stage seed; linked to Orbit Ventures", 3, 3)
    assert [(e["rank"], e["company"], e["shortlist_rank"]) for e in seed["entries"]] == [
        (1, "Beta", None), (2, "Beta", None), (3, "Gamma", None)]
    assert session.call("get_shortlist", sector=["fintech"], limit=1)["next_offset"] == 1
    grouped = session.call("get_shortlist", employees_min=20, employees_max=500, view="by_company")
    assert [(c["company"], c["roles_matching"]) for c in grouped["companies"]] == [("Alpha", 2), ("Beta", 2)]
    assert session.call("get_shortlist", role_location="remote")["roles_matching"] == 2
    assert session.call("get_shortlist", hq="Berlin")["entries"][0]["company"] == "Gamma"
    assert "is not a stage" in session.error("get_shortlist", stage=["unicorn"])
    found = session.call("search_roles", stage=["seed"], role_location="London")
    assert (found["total"], {r["company"] for r in found["roles"]}) == (2, {"Beta"})

    over = session.call("company_overview", company="beta", top_roles=1)
    assert (over["company"], over["backed_by"], len(over["top_roles"])) == ("Beta", ["Orbit Ventures"], 1)
    assert over["hiring_30_days"]["opened"] == 0 and over["profile"]["hq"]["value"] == "London, UK"

    todo = session.call("list_incomplete_profiles", fields=["stage", "hq"])
    assert (todo["total"], todo["companies"][0]["company"], todo["companies"][0]["missing"]) == (1, "Delta", ["stage", "hq"])
    assert "seed" in todo["stages"] and todo["sector_list"] == ["fintech", "health tech", "AI and ML"]
    profiles.set_field(world, "Delta", "hq", "Toronto")                       # by hand
    done = session.call("set_company_profile", company="Delta", stage="series A", hq="Paris",
                        sectors=["fintech"], employees=75)
    assert done["set"] == ["sectors", "stage", "employees"] and done["kept"] == {"hq": "set by hand; not replaced"}
    assert done["profile"]["hq"]["value"] == "Toronto" and done["profile"]["stage"]["source"] == "agent"
    assert "give at least one field" in session.error("set_company_profile", company="Delta")
    read_only = {t.name for t in Session(mcp_server.build_server(isolated_db_path, read_only=True)).tools()}
    assert {"company_overview", "list_incomplete_profiles"} <= read_only and "set_company_profile" not in read_only


# --- leveling ----------------------------------------------------------------------

LEVELED = SMALL_CONFIG + '\n[leveling]\nsenior_as_staff = ["alpha"]\n'


def test_a_senior_title_scores_as_staff_at_the_listed_companies(world, capsys, isolated_db_path):
    leveled = ranking.parse_config(LEVELED, "test")
    assert leveled.senior_as_staff == ["alpha"]
    plain = ranking.parse_config(SMALL_CONFIG, "test")
    alpha = {"title": "Senior Product Manager", "company": "Alpha", "location": "", "workplace_type": "",
             "date_posted": "", "first_seen_at": "2026-03-10T08:00:00Z"}
    today = day(10).date()
    assert ranking.evaluate(alpha, plain, today).score + 5 == ranking.evaluate(alpha, leveled, today).score   # 10 -> 15
    assert 'Seniority: "Senior" counts as "Staff" at Alpha (leveling) (+15)' in ranking.evaluate(alpha, leveled, today).detail
    # Elsewhere, and for a title that is already Staff, nothing changes.
    beta = {**alpha, "company": "Beta"}
    assert ranking.evaluate(beta, leveled, today).detail == ranking.evaluate(beta, plain, today).detail
    staff = {**alpha, "title": "Staff Product Manager"}
    assert 'Seniority: "Staff" (+15)' in ranking.evaluate(staff, leveled, today).detail

    # A config that wants the Staff title for more: the Senior title earns those title points too.
    staff_wanted = ranking.parse_config(LEVELED.replace(
        '"Group Product Manager" = 40', '"Group Product Manager" = 40\n"Staff Product Manager" = 45'), "test")
    as_written = ranking.evaluate({**alpha, "company": "Beta"}, staff_wanted, today)
    leveled_up = ranking.evaluate(alpha, staff_wanted, today)
    the_staff_title = ranking.evaluate({**staff, "company": "Beta"}, staff_wanted, today)
    assert leveled_up.score == the_staff_title.score == as_written.score + 15 + 5      # title 30 -> 45, seniority 10 -> 15
    assert ('Title counts as "Staff Product Manager" at Alpha (leveling), which matches "Staff Product Manager" (+45)'
            in leveled_up.detail)
    assert 'Seniority: "Senior" counts as "Staff" at Alpha (leveling) (+15)' in leveled_up.detail
    assert 'Title matches "Product Manager" (+30)' in as_written.detail
    # A Senior title that only the Staff phrase admits reaches the list at a leveled company, and nowhere else.
    only_staff = ranking.parse_config(
        '[title.wanted]\n"Staff Engineer" = 30\n[seniority.words]\n"Senior" = 10\n"Staff" = 15\n'
        '[leveling]\nsenior_as_staff = ["Alpha"]\n', "test")
    engineer = {**alpha, "title": "Senior Engineer, Payments"}
    assert ranking.evaluate(engineer, only_staff, today).eligible
    assert not ranking.evaluate({**engineer, "company": "Beta"}, only_staff, today).eligible
    # Leveling never lowers a score, never touches a title without "Senior", and never lifts an exclusion.
    lower = ranking.parse_config(LEVELED.replace('"Staff" = 15', '"Staff" = 5'), "test")
    assert 'Seniority: "Senior" (+10)' in ranking.evaluate(alpha, lower, today).detail
    assert staff_wanted.leveled_title("Product Manager", "Alpha") is None
    assert staff_wanted.leveled_title("Senior Product Manager", "Beta") is None
    assert staff_wanted.leveled_title("senior product manager", "ALPHA") == "Staff product manager"
    assert not ranking.evaluate({**alpha, "title": "Senior Product Manager Intern"}, staff_wanted, today).eligible

    (isolated_db_path.parent / "ranking.toml").write_text(LEVELED)
    a1 = world.execute("SELECT id FROM roles WHERE source_job_id = 'a1'").fetchone()["id"]
    assert cli.main(["why", str(a1)]) == 0
    assert 'Seniority: "Senior" counts as "Staff" at Alpha (leveling) (+15)' in capsys.readouterr().out
    # `why` shows both lines when the title is leveled as well.
    (isolated_db_path.parent / "ranking.toml").write_text(LEVELED.replace(
        '"Group Product Manager" = 40', '"Group Product Manager" = 40\n"Staff Product Manager" = 45'))
    assert cli.main(["why", str(a1)]) == 0
    out = capsys.readouterr().out
    assert 'Title counts as "Staff Product Manager" at Alpha (leveling), which matches "Staff Product Manager" (+45)' in out
    assert 'Seniority: "Senior" counts as "Staff" at Alpha (leveling) (+15)' in out
    for text, message in ((SMALL_CONFIG + '\n[leveling]\nsenior_as_staff = "Alpha"\n', "must be a list of company names"),
                          ('[leveling]\nsenior_as_staff = ["Alpha"]\n', 'needs both "Senior" and "Staff"')):
        with pytest.raises(ranking.ConfigError, match=message):
            ranking.parse_config(text)
    starter = ranking.starter_config()
    assert "[leveling]\nsenior_as_staff = []" in starter and ranking.parse_config(starter).senior_as_staff == []


# --- no tier anywhere --------------------------------------------------------------

def test_no_output_mentions_a_tier(world, capsys, isolated_db_path, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    with transaction(world):                                        # values an older version stored
        world.execute("UPDATE companies SET tier = 1")
    companies.add_company(world, "Epsilon", website="https://epsilon.example")
    from rolescout import resolve
    resolve.set_job_system(world, "Epsilon", "icims", "https://careers-epsilon.icims.com/jobs")
    a1 = str(world.execute("SELECT id FROM roles WHERE source_job_id = 'a1'").fetchone()["id"])
    shown = ""
    for argv in (["shortlist"], ["shortlist", "--by-company"], ["shortlist", "--stage", "seed"], ["why", a1],
                 ["companies", "list"], ["gaps"], ["brief", "icims"], ["company", "Alpha"], ["status"], ["digest"],
                 ["companies", "list", "--json"], ["shortlist", "--json"], ["why", a1, "--json"], ["gaps", "--json"]):
        cli.main(argv)
        shown += capsys.readouterr().out
    assert shown and not re.search(r"\btier\b|\bT[1-9]\b", shown, re.I)
    assert "tier" not in json.dumps(export_companies(world))
    # Nothing writes it either: a company added now has none, whatever old flags say.
    assert cli.main(["companies", "add", "Zeta", "--tier", "2"]) == 0
    assert world.execute("SELECT tier FROM companies WHERE name = 'Zeta'").fetchone()["tier"] is None
    assert "--tier" not in cli.build_parser().format_help()

    pytest.importorskip("mcp")
    from rolescout import mcp_server
    from tests.test_mcp import Session
    session = Session(mcp_server.build_server(isolated_db_path))
    results = [session.call("get_shortlist"), session.call("get_shortlist", view="by_company"),
               session.call("get_role", role_id=int(a1)), session.call("explain_rank", role_id=int(a1)),
               session.call("list_target_companies"), session.call("coverage_gaps"),
               session.call("connector_brief", system="icims"), session.call("company_overview", company="Alpha"),
               session.call("hiring_trends"), session.call("add_target_company", name="Eta")]
    assert not re.search(r"\btier\b", json.dumps(results), re.I)
    described = " ".join((t.description or "") + json.dumps(t.input_schema) for t in session.tools())
    assert not re.search(r"\btier\b", described, re.I)


# --- export and import -------------------------------------------------------------

def test_export_import_keeps_manual_values_the_vc_list_and_manual_links(world, tmp_path, capsys):
    profiles.set_field(world, "Alpha", "stage", "Series C")                   # by hand, over the agent's value
    profiles.set_field(world, "Alpha", "description", "Alpha does payments.")
    profiles.set_field(world, "Gamma", "sectors", "fintech, health tech")
    portfolio.add_board(world, "https://jobs.orbit.example", "Orbit Ventures", vc="Orbit Ventures")
    data = export_companies(world)
    assert [(v["name"], v["short_name"], v["type"], v["boards"]) for v in data["vcs"]] == [
        ("City Tech Alliance", "City Tech Alliance", "ecosystem", []),
        ("Orbit Ventures", "Orbit", "vc", ["https://jobs.orbit.example"])]
    by_name = {c["name"]: c for c in data["companies"]}
    assert set(by_name["Alpha"]["profile"]) == {"stage", "description"}       # manual values only
    assert by_name["Alpha"]["profile"]["stage"]["value"] == "series C"
    assert "profile" not in by_name["Beta"]                                   # an agent's values stay behind
    assert [l["vc"] for l in by_name["Beta"]["vcs"]] == ["Orbit Ventures"]

    target = db.connect(tmp_path / "other.db")
    portfolio.add_board(target, "https://jobs.orbit.example")                 # registered there under a host name
    result = import_companies(target, json.loads(json.dumps(data)))
    assert (result["vcs_added"], result["profile_values"], result["vc_links"]) == (2, 3, 3)
    alpha = profiles.get_profile(target, "Alpha")
    assert (alpha["stage"]["value"], alpha["stage"]["source"]) == ("series C", "manual")
    assert (alpha["description"]["value"], alpha["description"]["source"]) == ("Alpha does payments.", "manual")
    assert alpha["employees"]["value"] is None
    assert profiles.get_profile(target, "Gamma")["sectors"]["value"] == ["fintech", "health tech"]
    listed = {v["name"]: v for v in vcs.list_vcs(target)}
    assert (listed["City Tech Alliance"]["type"], listed["Orbit Ventures"]["short_name"]) == ("ecosystem", "Orbit")
    assert listed["Orbit Ventures"]["boards"] == ["https://jobs.orbit.example"]      # the board follows its VC
    ids = {r["name"]: r["id"] for r in target.execute("SELECT id, name FROM companies")}
    assert companies.backed_by(target) == {ids["Beta"]: ["Orbit"], ids["Gamma"]: ["Orbit"]}   # the ecosystem link is kept, not shown
    assert [r["source"] for r in target.execute("SELECT source FROM company_vcs")] == ["manual"] * 3
    # A second import changes nothing, and a later backfill leaves it all alone.
    again = import_companies(target, data)
    assert (again["vcs_added"], again["vcs_updated"]) == (0, 2)
    with transaction(target):
        profiles.backfill(target)
    assert profiles.get_profile(target, "Alpha") == alpha

    out_file = tmp_path / "private" / "rolescout-companies.json"
    assert cli.main(["companies", "export", str(out_file)]) == 0
    assert "With them: 2 VCs, 3 profile values set by hand, 3 VC links made by hand." in capsys.readouterr().out
