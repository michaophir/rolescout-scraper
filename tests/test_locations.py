"""Location text: splitting, the United States forms, the shortlist requirement, the display."""

import json

import pytest

from rolescout import cli, companies
from rolescout import shortlist as ranking
from rolescout.locations import Requirement, in_united_states, names_no_place, split_locations
from tests.test_shortlist import SMALL_CONFIG, cfg, day, mk, role_id, world  # noqa: F401  (world is a fixture)

REQUIRE_US = SMALL_CONFIG.replace("size = 3", "size = 20").replace(
    "[location]\nremote_points = 5", '[location]\nremote_points = 5\nrequire = ["United States"]')


def test_the_requirement_line_is_in_the_test_config():
    assert 'require = ["United States"]' in REQUIRE_US
    assert cfg(REQUIRE_US).required_locations == ["United States"]
    assert cfg().required_locations == []                      # off unless set


# --- splitting ---------------------------------------------------------------

@pytest.mark.parametrize("text, places", [
    ("Austin, TX | New York City, NY", ["Austin, TX", "New York City, NY"]),
    ("London; New York, NY", ["London", "New York, NY"]),
    ("Denver, CO / Seattle, WA / Remote", ["Denver, CO", "Seattle, WA", "Remote"]),
    ("San Jose, CA, New York, NY, Portland, OR, or Remote within Canada or United States",
     ["San Jose, CA", "New York, NY", "Portland, OR", "Remote within Canada", "United States"]),
    ("Seattle, San Francisco, New York", ["Seattle", "San Francisco", "New York"]),
    ("New York, New York, United States", ["New York, New York, United States"]),
    ("San Francisco, California, United States", ["San Francisco, California, United States"]),
    ("London, UK", ["London, UK"]),
    ("Remote - California; Remote - Virginia", ["Remote - California", "Remote - Virginia"]),
    ("Austin, TX | Austin, TX", ["Austin, TX"]),
    ("SF, NY, SEA, Remote-US", ["SF", "NY", "SEA", "Remote-US"]),
    ("NYC, SF, Seattle, US - Remote", ["NYC", "SF", "Seattle", "US - Remote"]),
    ("New York, NY (HQ)", ["New York, NY (HQ)"]),
    ("San Francisco, CA, USA", ["San Francisco, CA, USA"]),
    ("", []), (None, []),
])
def test_split_locations(text, places):
    assert split_locations(text) == places


# --- the United States, in the forms boards use ------------------------------

@pytest.mark.parametrize("place", [
    "United States", "USA", "US", "U.S.", "Remote - US", "Remote (United States)", "US-Remote",
    "New York, NY", "NYC", "San Francisco", "SF", "Austin, Texas", "Chicago, IL", "Boise, ID",
    "California", "Washington, DC", "Remote - California", "Honolulu, HI", "Bay Area",
])
def test_united_states_forms_match(place):
    assert in_united_states(place), place


@pytest.mark.parametrize("place", [
    "London", "London, UK", "Toronto, Canada", "Remote - Canada", "Tel Aviv, IL", "Berlin, DE",
    "Bengaluru, IN", "Paris, France", "Remote (EMEA)", "Mexico City, MX", "Sydney", "join us today",
    "Dublin, Ireland", "Singapore",
])
def test_places_outside_the_united_states_do_not_match(place):
    assert not in_united_states(place), place


def test_requirement_accepts_unplaced_remote_and_literal_places():
    us = Requirement(["United States"])
    for remote in ("Remote", "Anywhere", "Remote-Friendly (Travel-Required)", "Fully remote", "Work from home"):
        assert names_no_place(remote) and us.accepts(remote), remote
    for placed in ("Remote - Canada", "Remote (EMEA)", "Remote, Germany"):
        assert not names_no_place(placed) and not us.accepts(placed), placed
    assert us.first_match(["London", "Toronto", "Denver, CO"]) == "Denver, CO"
    both = Requirement(["USA", "London"])
    assert both.accepts("London, UK") and both.accepts("Austin, TX") and not both.accepts("Paris")
    assert both.describe() == "USA, London" and not Requirement([])


# --- the shortlist requirement -----------------------------------------------

def build(conn, world, text=REQUIRE_US):
    world("Alpha", [
        # Eight jobs, each with a title of its own: with one title, descriptions
        # that differ only in the place ("About london", "About austin") would
        # make some of them one job posted per city.
        mk("london", "Product Manager, Ads", "London, UK"),
        mk("both", "Product Manager, Billing", "London | New York, NY"),
        mk("canada", "Product Manager, Cloud", "Remote - Canada"),
        mk("remote", "Product Manager, Data", "Remote"),
        mk("remote-flag", "Product Manager, Events", "", workplace="remote"),
        mk("austin", "Product Manager, Fraud", "Austin, TX"),
        mk("blank", "Product Manager, Growth", ""),
        mk("telaviv", "Product Manager, Health", "Tel Aviv, IL"),
    ], on_day=10)
    ranking.build_shortlist(conn, cfg(text), now=day(10))
    entries = ranking.shortlist_entries(conn, ranking.latest_shortlist(conn)["id"], cfg(text))
    return {conn.execute("SELECT source_job_id FROM roles WHERE id = ?", (e["id"],)).fetchone()[0]: e
            for e in entries}


def test_location_requirement_decides_who_reaches_the_shortlist(conn, world):
    listed = build(conn, world)
    # London only: dropped. London and New York: kept. Remote in Canada: dropped.
    # Remote with no country: kept, whether the text or the board's flag says remote.
    assert set(listed) == {"both", "remote", "remote-flag", "austin"}

    def why(job):
        return ranking.explain(conn, role_id(conn, job), cfg(REQUIRE_US), now=day(10))
    london = why("london")
    assert london["rank"] is None
    assert london["why_not"] == "none of its locations (London, UK) is in the places you require (United States)"
    assert "Location requirement not met (London, UK)" in london["detail"]
    assert why("canada")["why_not"] == (
        "none of its locations (Remote - Canada) is in the places you require (United States)")
    assert why("telaviv")["why_not"].startswith("none of its locations (Tel Aviv, IL)")
    assert why("blank")["why_not"] == "it gives no location, and you require one in United States"
    kept = why("both")
    assert kept["rank"] is not None and "Location requirement met by New York, NY" in kept["detail"]
    assert "Location requirement met by Remote" in why("remote")["detail"]
    assert "Location requirement met by a remote role with no place given" in why("remote-flag")["detail"]


def test_requirement_is_off_by_default(conn, world):
    listed = build(conn, world, SMALL_CONFIG.replace("size = 3", "size = 20\nmax_per_company = 0"))
    assert len(listed) == 8                                    # nothing dropped for its location
    info = ranking.explain(conn, role_id(conn, "london"), cfg(), now=day(10))
    assert not any("requirement" in line for line in info["detail"])


def test_why_command_explains_a_role_kept_off_by_location(conn, world, capsys, isolated_db_path):
    build(conn, world)
    (isolated_db_path.parent / "ranking.toml").write_text(REQUIRE_US)
    assert cli.main(["why", str(role_id(conn, "london"))]) == 0
    out = capsys.readouterr().out
    assert ("Not on the shortlist: none of its locations (London, UK) is in the places you require "
            "(United States).") in out
    assert "Location requirement not met (London, UK)" in out


def test_require_must_be_a_list_and_the_starter_shows_how_to_turn_it_on():
    with pytest.raises(ranking.ConfigError, match="location.require must be a list of places"):
        cfg('[location]\nrequire = "United States"\n')
    starter = ranking.starter_config()
    assert '# require = ["United States"]' in starter
    assert cfg(starter).required_locations == []               # commented out: off
    on = cfg(starter.replace('# require = ["United States"]', 'require = ["United States"]'))
    assert on.required_locations == ["United States"]


# --- the location shown in the table -----------------------------------------

def test_display_leads_with_the_location_that_earned_the_points(conn, world, capsys, isolated_db_path):
    config = SMALL_CONFIG.replace("size = 3", "size = 20")     # "New York" earns 10, remote 5
    (isolated_db_path.parent / "ranking.toml").write_text(config)
    world("Alpha", [
        mk("multi", "Senior Product Manager", "Austin, TX | New York, NY | London"),
        mk("remote-multi", "Product Manager", "Denver, CO; Remote; Boston, MA"),
        mk("plain-multi", "Staff Product Manager", "Austin, TX | Denver, CO"),
        mk("single", "Group Product Manager", "Chicago, IL"),
    ], on_day=10)
    ranking.build_shortlist(conn, cfg(config), now=day(10))
    entries = {e["title"]: e for e in ranking.shortlist_entries(
        conn, ranking.latest_shortlist(conn)["id"], cfg(config))}
    # Listed first by the board: Austin. Earned the points: New York.
    assert entries["Senior Product Manager"]["location_shown"] == "New York, NY +2 more"
    assert entries["Senior Product Manager"]["location"] == "Austin, TX | New York, NY | London"   # stored text unchanged
    assert entries["Product Manager"]["location_shown"] == "Remote +2 more"           # remote earned them
    assert entries["Staff Product Manager"]["location_shown"] == "Austin, TX +1 more"  # nothing earned: first listed
    assert entries["Group Product Manager"]["location_shown"] == "Chicago, IL"

    assert cli.main(["shortlist"]) == 0
    out = capsys.readouterr().out
    assert "New York, NY +2 more" in out and "Austin, TX | New York" not in out

    # With a requirement and no points earned, the place that met it leads.
    required = cfg(REQUIRE_US.replace('"New York" = 10', '"Nowhere" = 10'))
    role = {"location": "London | Toronto | Denver, CO", "workplace_type": ""}
    assert ranking.display_location(role, required) == "Denver, CO +2 more"
    assert ranking.display_location(role, None) == "London +2 more"                    # no config: first listed
    # Remote earned the points, but only the US remote place meets the requirement.
    role = {"location": "Remote - Canada | Remote - US | Austin, TX", "workplace_type": ""}
    assert ranking.display_location(role, required) == "Remote - US +2 more"
    role = {"location": "Austin, TX, or Remote within Canada", "workplace_type": ""}
    assert ranking.display_location(role, required) == "Austin, TX +1 more"


# --- set-website -------------------------------------------------------------

def test_set_website_command(conn, capsys):
    companies.add_company(conn, "Acme", website="https://wrong.example")
    assert cli.main(["companies", "set-website", "acme", "WWW.Acme.example/about"]) == 0
    out = capsys.readouterr().out
    assert "Acme: website changed from https://wrong.example to https://www.acme.example/about." in out
    assert 'rolescout companies resolve "Acme"' in out
    assert companies.get_company(conn, "Acme")["website"] == "https://www.acme.example/about"

    assert cli.main(["companies", "set-website", "Acme", "https://www.acme.example/about"]) == 0
    assert "website is already https://www.acme.example/about" in capsys.readouterr().out
    assert cli.main(["companies", "set-website", "Acme", "not a website"]) == 1
    assert "Acme: 'not a website' is not a website" in capsys.readouterr().err
    assert companies.get_company(conn, "Acme")["website"] == "https://www.acme.example/about"   # unchanged
    assert cli.main(["companies", "set-website", "Nope", "acme.example"]) == 1
    assert "no company named 'Nope'" in capsys.readouterr().err
    assert cli.main(["companies", "set-website", "Acme", ""]) == 0                              # clears it
    assert companies.get_company(conn, "Acme")["website"] == ""


def test_corrected_website_links_a_portfolio_candidate(conn):
    from rolescout import candidates as cands
    from rolescout.db import transaction
    companies.add_company(conn, "Acme", website="https://wrong.example")
    with transaction(conn):
        conn.execute("INSERT INTO candidates (domain, name, first_seen_at) VALUES ('acme.example', 'Acme Inc', 'x')")
    config = cands.parse_config("")
    cands.apply_filter(conn, config)
    assert cands.get_candidate(conn, "acme.example")["state"] != "tracked"
    companies.set_website(conn, "Acme", "acme.example")
    cands.apply_filter(conn, config)
    assert cands.get_candidate(conn, "acme.example")["state"] == "tracked"


def test_set_company_website_mcp_tool(conn, isolated_db_path):
    pytest.importorskip("mcp")
    from rolescout import mcp_server
    from tests.test_mcp import Session
    companies.add_company(conn, "Acme", website="https://wrong.example")
    session = Session(mcp_server.build_server(isolated_db_path))
    result = session.call("set_company_website", name="acme", website="acme.example")
    assert result == {"name": "Acme", "website": "https://acme.example",
                      "previous_website": "https://wrong.example", "changed": True}
    assert "is not a website" in session.error("set_company_website", name="Acme", website="n/a")
    assert "no company named" in session.error("set_company_website", name="Nope", website="acme.example")
    read_only = Session(mcp_server.build_server(isolated_db_path, read_only=True))
    assert "set_company_website" not in {t.name for t in read_only.tools()}
    assert json.dumps(companies.get_company(conn, "Acme")["website"]) == '"https://acme.example"'
