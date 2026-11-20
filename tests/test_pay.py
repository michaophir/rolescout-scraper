"""Pay: read from a board's pay field or found in a description, stored with
its source, kept up to date by every fetch, and shown wherever roles are."""

import json

import pytest

from rolescout import cli, companies, db, overview, pay, scout
from rolescout import shortlist as ranking
from rolescout.db import transaction
from rolescout.pay import Pay, from_description, from_field
from rolescout.query import query_roles
from rolescout.scout import apply_fetch
from tests.test_db import add_roles, at_version
from tests.test_shortlist import SMALL_CONFIG, cfg, day, mk, role_id, stamp, world  # noqa: F401  (world is a fixture)

USD_YEAR = dict(currency="USD", period="year")


# --- from a description -----------------------------------------------------------

def test_pay_is_found_in_real_description_snippets_and_false_positives_are_ignored(load_fixture):
    snippets = load_fixture("pay_snippets_from_descriptions.json")
    kinds = " | ".join(s["about"] for s in snippets)
    for needed in ("a single range", "two ranges by location", "hourly pay", "K notation", "false positive"):
        assert needed in kinds
    for snippet in snippets:
        found = from_description(snippet["text"])
        got = None if found is None else {"min": found.low, "max": found.high, "currency": found.currency,
                                          "period": found.period, "several_ranges": found.varies}
        assert got == snippet["expect"], snippet["about"]
        assert snippet["about"].startswith("false positive") == (snippet["expect"] is None)


@pytest.mark.parametrize("text, low, high", [
    ("Salary range: $180K–$220K.", 180_000, 220_000),
    ("The base salary is $180–220K depending on level.", 180_000, 220_000),
    ("Compensation: $180k - 220k", 180_000, 220_000),
    ("The pay range for this role is USD 90,000 to 110,000 per year.", 90_000, 110_000),
    ("Annual base salary between $120,000 and $150,000.", 120_000, 150_000),
    ("We offer $120K–$150K base salary plus equity.", 120_000, 150_000),              # the pay word right after
    ("Benefits include health cover. The salary range is $95,000.00 - $105,500.50.", 95_000, 105_500.5),
    ("Your total compensation (salary + commission) will be $150,000 - $200,000.", 150_000, 200_000),
])
def test_ranges_in_the_forms_companies_write_them(text, low, high):
    assert from_description(text) == Pay(low, high, "USD", "year")


@pytest.mark.parametrize("text", [
    "",
    "The base salary for this role is $150,000.",                                  # a lone figure
    "Salary: up to $200K, depending on experience.",
    "We raised $50M-$60M in our Series B and pay competitively.",
    "Our customers have $100,000 - $500,000 in annual revenue.",
    "You will own a book of business of $2,000,000 - $3,000,000.",
    "We closed a funding round of $20,000,000 to $25,000,000 last year.",
    "Competitive salary. Tuition reimbursement of $5,000 - $10,000 per year.",
    "Base salary plus an annual bonus of $20,000 - $40,000.",
    "Benefits: a home office stipend of $500-$1,000 and a 401(k) match of $2,000 - $4,000.",
    "A sign-on bonus between $25,000 and $50,000 is offered for this role.",
    "Manage a budget of $250,000 - $400,000 and report on spend.",
    "The salary is competitive. Projects range from $50,000 - $500,000 in size.",   # ten times apart, and no pay word in the sentence
    "Salary range: $150,000 - $90,000.",                                            # upside down
    "Salary range: $40,000 - $400,000.",                                            # too far apart to be one job's pay
    "The pay range is $4,000 - $5,000 per month.",
    "Pay: $25 - $30.",                                                              # small, and it does not say hourly
    "The salary range is 120,000 - 150,000.",                                       # no currency
    "Founded in 2019-2020, we pay well.",
])
def test_nothing_is_read_where_it_is_not_clearly_a_pay_range(text):
    assert from_description(text) is None


def test_several_ranges_give_the_lowest_and_highest_and_say_so():
    by_place = from_description("The base salary range is $150,000 - $180,000 in New York and "
                                "$130,000 - $160,000 in Denver, plus equity.")
    assert by_place == Pay(130_000, 180_000, "USD", "year", varies=True)
    assert by_place.text() == "USD 130,000 - 180,000 per year (several ranges, by location or level)"
    assert by_place.short() == "USD 130K-180K/yr*"
    # The same range said twice is one range.
    assert from_description("Salary: $100,000 - $120,000. In Colorado the salary range is $100,000 - $120,000.") \
        == Pay(100_000, 120_000, "USD", "year")
    # Lines of a list take their meaning from what introduces the list.
    listed = "Pay ranges by location:\nNew York: $150,000 - $180,000\nDenver: $130,000 - $160,000\nBenefits: 401(k)"
    assert from_description(listed) == Pay(130_000, 180_000, "USD", "year", varies=True)
    # Another currency beside the first one is left out, not mixed in.
    assert from_description("Salary range: £70,000 - £85,000 (UK) or $110,000 - $130,000 (US).") \
        == Pay(70_000, 85_000, "GBP", "year")
    assert from_description("The hourly rate is $22.50 - $28 per hour.").text() == "USD 22.5 - 28 per hour"
    assert from_description("Canada base pay range: $153,000 - $213,000").currency == "CAD"


# --- from a board's pay field --------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("$190,000 — $230,000 USD", Pay(190_000, 230_000, "USD", "year")),
    ("USD 80,000 - 165,000 per year", Pay(80_000, 165_000, "USD", "year")),
    ("$211.4K – $290.6K • Offers Equity", Pay(211_400, 290_600, "USD", "year")),
    ("USD 185,000 - 240,000 per year (US); CAD 160,000 - 200,000 per year (CA)", Pay(185_000, 240_000, "USD", "year")),
    ("USD 150,000 - 180,000 per year (NY); USD 130,000 - 160,000 per year (CO)",
     Pay(130_000, 180_000, "USD", "year", varies=True)),
    ("EUR 60 per hour", Pay(60, 60, "EUR", "hour")),
    ("$42.31 — $56.25 USD", Pay(42.31, 56.25, "USD", "hour")),          # a small amount in a pay field is hourly
    ("€4,000 - €5,000 per month", None),
    ("5,000 one time", None),
    ("Up to €90k", Pay(90_000, 90_000, "EUR", "year")),
])
def test_the_numbers_in_a_pay_field(text, expected):
    assert from_field(text) == expected


def test_the_field_wins_and_a_role_without_one_takes_pay_from_its_description():
    described = "The salary range for this role is $120,000 - $150,000."
    assert pay.columns("$190,000 — $230,000 USD", described) == (
        "$190,000 — $230,000 USD", 190_000, 230_000, "USD", "year", 0, "field")
    assert pay.columns("", described) == ("USD 120,000 - 150,000 per year", 120_000, 150_000, "USD", "year", 0, "description")
    # A field that cannot be read as numbers is still the board's word, shown as written.
    assert pay.columns("€4,000 - €5,000 per month", described) == (
        "€4,000 - €5,000 per month", None, None, "", "", 0, "field")
    assert pay.columns("", "We build things.") == pay.NO_PAY == pay.columns("", None)


# --- stored by a fetch ------------------------------------------------------------

def stored_pay(conn, job_id):
    row = conn.execute("SELECT compensation_text, pay_min, pay_max, pay_currency, pay_period, pay_varies, pay_source "
                       "FROM roles WHERE source_job_id = ?", (job_id,)).fetchone()
    return tuple(row)


def test_a_fetch_stores_pay_with_its_source_and_keeps_it_current(conn, world):
    described = "About the role. The salary range for this role is $120,000 - $150,000."
    world("Alpha", [
        mk("described", "Product Manager", text=described),
        mk("none", "Product Manager, Ads", text="About the role."),
    ], on_day=10)
    assert stored_pay(conn, "described") == ("USD 120,000 - 150,000 per year", 120_000, 150_000, "USD", "year", 0, "description")
    assert stored_pay(conn, "none") == pay.NO_PAY

    # A fetch that brings no description (a source that charges a request
    # for each fetches it once) leaves the pay read from the stored one.
    world("Alpha", [mk("described", "Product Manager", text=""), mk("none", "Product Manager, Ads", text="")], on_day=11)
    assert stored_pay(conn, "described")[1:] == (120_000, 150_000, "USD", "year", 0, "description")
    assert stored_pay(conn, "none") == pay.NO_PAY

    # The description changes: the pay follows it, and goes when the text no longer states any.
    world("Alpha", [mk("described", "Product Manager", text=described.replace("150,000", "165,000")),
                    mk("none", "Product Manager, Ads", text="About the role.")], on_day=12)
    assert stored_pay(conn, "described")[:3] == ("USD 120,000 - 165,000 per year", 120_000, 165_000)
    world("Alpha", [mk("described", "Product Manager", text="About the role, rewritten."),
                    mk("none", "Product Manager, Ads", text="About the role.")], on_day=13)
    assert stored_pay(conn, "described") == pay.NO_PAY

    # Once the board fills in its pay field, the field wins over the description; when it is emptied, the description is read again.
    from dataclasses import replace
    with_field = replace(mk("described", "Product Manager", text=described), compensation_text="$200,000 — $240,000 USD")
    world("Alpha", [with_field, mk("none", "Product Manager, Ads", text="About the role.")], on_day=14)
    assert stored_pay(conn, "described") == ("$200,000 — $240,000 USD", 200_000, 240_000, "USD", "year", 0, "field")
    world("Alpha", [mk("described", "Product Manager", text=described),
                    mk("none", "Product Manager, Ads", text="About the role.")], on_day=15)
    assert stored_pay(conn, "described")[-1] == "description"

    # A role that closes loses its description and keeps its pay.
    for on_day in (16, 17):
        world("Alpha", [mk("none", "Product Manager, Ads", text="About the role.")], on_day=on_day)
    closed = conn.execute("SELECT r.closed_at, d.description FROM roles r LEFT JOIN role_descriptions d ON d.role_id = r.id "
                          "WHERE r.source_job_id = 'described'").fetchone()
    assert closed["closed_at"] and closed["description"] is None
    assert stored_pay(conn, "described")[1:3] == (120_000, 150_000)


def test_upgrade_backfills_pay_from_stored_fields_and_descriptions_without_a_network(isolated_db_path, monkeypatch):
    old = at_version(monkeypatch, 12)
    add_roles(old, {
        ("greenhouse", "field"): ("$190,000 — $230,000 USD", "The salary range for this role is $120,000 - $150,000."),
        ("greenhouse", "described"): ("", "The salary range for this role is $120,000 - $150,000."),
        ("ashby", "zones"): ("", "Base salary: $150,000 - $180,000 in New York and $130,000 - $160,000 in Denver."),
        ("greenhouse", "nothing"): ("", "We raised $50M-$60M. Annual bonus of $10,000 - $20,000."),
        ("workday", "bare"): ("", None),
    })
    old.close()

    conn = db.connect()         # the no_network fixture fails any connection attempt
    assert db.schema_version(conn) == db.SCHEMA_VERSION >= 13
    assert stored_pay(conn, "field") == ("$190,000 — $230,000 USD", 190_000, 230_000, "USD", "year", 0, "field")
    assert stored_pay(conn, "described") == ("USD 120,000 - 150,000 per year", 120_000, 150_000, "USD", "year", 0, "description")
    assert stored_pay(conn, "zones") == ("USD 130,000 - 180,000 per year (several ranges, by location or level)",
                                         130_000, 180_000, "USD", "year", 1, "description")
    assert stored_pay(conn, "nothing") == stored_pay(conn, "bare") == pay.NO_PAY
    row = conn.execute("SELECT pay_min, pay_max, pay_source FROM v_open_roles WHERE compensation_text LIKE 'USD 120%'").fetchone()
    assert tuple(row) == (120_000, 150_000, "description")


# --- shown ------------------------------------------------------------------------

@pytest.fixture
def paid(conn, world, isolated_db_path):
    (isolated_db_path.parent / "ranking.toml").write_text(SMALL_CONFIG)
    from dataclasses import replace
    world("Alpha", [
        replace(mk("field", "Senior Product Manager", text="We use Kubernetes and Postgres."),
                compensation_text="$190,000 — $230,000 USD"),
        mk("described", "Product Manager", text="Payments team. The salary range is $150,000 - $180,000 in New York "
                                                "and $130,000 - $160,000 in Denver."),
        mk("bare", "Product Manager, Ads", text="Advertising team, working with Kubernetes."),
    ], on_day=10)
    ranking.build_shortlist(conn, cfg(), now=day(10))
    return conn


def test_shortlist_why_and_overview_show_pay_and_where_it_came_from(paid, capsys):
    assert cli.main(["shortlist"]) == 0
    out = capsys.readouterr().out
    assert "Pay" in out.splitlines()[2]
    assert "USD 190K-230K/yr (board)" in out and "USD 130K-180K/yr* (description)" in out
    assert "(description) was found in the description text" in out
    assert cli.main(["shortlist", "--json"]) == 0
    entries = {e["title"]: e for e in json.loads(capsys.readouterr().out)["entries"]}
    assert (entries["Product Manager"]["pay_min"], entries["Product Manager"]["pay_max"],
            entries["Product Manager"]["pay_source"], entries["Product Manager"]["pay_varies"]) == (130_000, 180_000, "description", 1)
    assert entries["Product Manager, Ads"]["compensation_text"] == ""
    assert cli.main(["shortlist", "--location", "Austin"]) == 0
    assert "USD 190K-230K/yr (board)" in capsys.readouterr().out

    assert cli.main(["why", str(role_id(paid, "described"))]) == 0
    assert ("Pay: USD 130,000 - 180,000 per year (several ranges, by location or level) "
            "(found in the description)") in capsys.readouterr().out
    assert cli.main(["why", str(role_id(paid, "field"))]) == 0
    assert "Pay: $190,000 — $230,000 USD (the board's pay field)" in capsys.readouterr().out
    assert cli.main(["why", str(role_id(paid, "bare"))]) == 0
    assert "Pay: none stated" in capsys.readouterr().out

    top = {r["title"]: r["compensation"] for r in overview.company_overview(paid, "Alpha", cfg(), now=day(10))["top_roles"]}
    assert top["Product Manager"] == {"text": "USD 130,000 - 180,000 per year (several ranges, by location or level)",
                                      "min": 130_000, "max": 180_000, "currency": "USD", "period": "year",
                                      "several_ranges": True, "source": "description"}
    assert top["Senior Product Manager"]["source"] == "field" and top["Product Manager, Ads"] is None
    assert cli.main(["company", "Alpha"]) == 0
    out = capsys.readouterr().out
    assert "USD 190K-230K/yr (board)" in out and "USD 130K-180K/yr* (description)" in out


def test_query_and_mcp_search_match_text_in_the_description(paid, capsys, isolated_db_path):
    found = query_roles(paid, text=["kubernetes"])
    assert [(r["title"], r["excerpt"]) for r in found] == [
        ("Product Manager, Ads", "Advertising team, working with Kubernetes."),
        ("Senior Product Manager", "We use Kubernetes and Postgres.")]
    assert [r["title"] for r in query_roles(paid, text=["payments", "postgres"])] == ["Product Manager", "Senior Product Manager"]
    assert [r["title"] for r in query_roles(paid, text=["kubernetes"], title=["senior"])] == ["Senior Product Manager"]
    assert query_roles(paid, text=["100%"]) == [] and "excerpt" not in query_roles(paid)[0]
    # A long description comes back as the text around what was found.
    long = "Intro. " * 40 + "We run Kubernetes clusters at scale. " + "Outro. " * 40
    paid.execute("UPDATE role_descriptions SET description = ? WHERE role_id = ?", (long, role_id(paid, "bare")))
    excerpt = query_roles(paid, text=["kubernetes"])[0]["excerpt"]
    assert excerpt.startswith("…") and excerpt.endswith("…") and "We run Kubernetes clusters" in excerpt and len(excerpt) < 180

    assert cli.main(["query", "--text", "postgres"]) == 0
    out = capsys.readouterr().out
    assert "In the description" in out and "We use Kubernetes and Postgres." in out
    assert "USD 190K-230K/yr (board)" in out and out.endswith("\n1 role.\n")
    assert cli.main(["query", "--text", "nothing like this"]) == 0
    assert capsys.readouterr().out.endswith("\n0 roles.\n")

    # A closed role has no description left to search.
    with transaction(paid):
        for on_day in (11, 12):
            apply_fetch(paid, *paid.execute("SELECT company_id, id FROM boards").fetchone(), [], stamp(on_day))
    assert query_roles(paid, text=["kubernetes"], status="all") == []

    pytest.importorskip("mcp")
    from rolescout import mcp_server
    from tests.test_mcp import Session
    session = Session(mcp_server.build_server(isolated_db_path))
    everything = session.call("search_roles", status="all")
    assert {r["title"]: (r["compensation"] or {}).get("source") for r in everything["roles"]} == {
        "Senior Product Manager": "field", "Product Manager": "description", "Product Manager, Ads": None}
    assert session.call("search_roles", status="all", description_contains=["kubernetes"])["total"] == 0
    role = session.call("get_role", role_id=role_id(paid, "described"))
    assert role["compensation"]["several_ranges"] is True and (role["compensation"]["min"], role["compensation"]["max"]) == (130_000, 180_000)
    assert session.call("explain_rank", role_id=role_id(paid, "field"))["compensation"]["text"] == "$190,000 — $230,000 USD"


# --- a Greenhouse pay field with several zones -------------------------------------

def test_every_zone_of_a_greenhouse_pay_field_is_read(conn, load_fixture, fake_session):
    from rolescout.adapters import GreenhouseAdapter
    board = load_fixture("greenhouse_pay_field_with_zones.json")
    roles = GreenhouseAdapter(fake_session(board)).fetch("acme")
    texts = {r.title.strip(): r.compensation_text for r in roles}
    # Each zone after its title, all of them, in the board's order.
    assert texts["Business Systems Analyst, IT Vendor Operations"] == (
        "Zone A: $83,900 — $125,900 USD; Zone B: $78,100 — $117,100 USD; "
        "Zone C: $71,400 — $107,000 USD; Zone D: $63,000 — $94,400 USD")
    assert texts["Chargé de Compte PME"] == "$86,000 — $129,000 CAD"          # one range: as before, no title
    # Zones written in the block's text after a long explanation: the figures are kept.
    lead = texts["Enterprise Sales Lead, US"]
    assert lead.startswith("…") and "Zone D: $319,040-$478,560" in lead and len(lead) <= 500

    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "greenhouse", "acme")
    scout.run_scout(conn, adapter_for=lambda source: GreenhouseAdapter(fake_session(board)))
    stored = {r["title"].strip(): tuple(r)[1:] for r in conn.execute(
        "SELECT title, pay_min, pay_max, pay_currency, pay_period, pay_varies, pay_source FROM roles")}
    assert stored == {
        # Zones that differ: the lowest and the highest, marked as several ranges.
        "Business Systems Analyst, IT Vendor Operations": (63_000, 125_900, "USD", "year", 1, "field"),
        "SMB Account Executive": (30.07, 56.38, "USD", "hour", 1, "field"),
        # Four zones with the same range are one range.
        "Chief Compliance Officer, Acme Pay Investing": (217_800, 326_800, "USD", "year", 0, "field"),
        "Enterprise Sales Lead, US": (319_040, 478_560, "USD", "year", 0, "field"),
        "Chargé de Compte PME": (86_000, 129_000, "CAD", "year", 0, "field"),
    }
    row = conn.execute("SELECT * FROM roles WHERE title LIKE 'Business Systems Analyst%'").fetchone()
    assert pay.shown(row) == "USD 63K-125.9K/yr* (board)"
    assert pay.summary(row)["several_ranges"] is True
