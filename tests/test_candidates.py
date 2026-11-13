"""Portfolio boards as a source of companies: harvest, identity, filter rules, activation."""

import json
from pathlib import Path

import pytest

from rolescout import candidates as cands
from rolescout import cli, companies, db, portfolio, scout
from rolescout.portfolio import Fetcher, jobs_filter_path
from tests.conftest import FakeWeb
from tests.test_scout import Scripted, role

REPO = Path(__file__).parent.parent
FIXTURE = json.loads((REPO / "tests" / "fixtures" / "getro_board_pages.json").read_text())
LOCATIONS = ["United States", "Remote"]

CONFIG = """
[size]
min_employees = 10
unknown = "review"

[stage]
accept = ["seed", "series_a", "series_b", "series_unknown", "ipo"]
reject = ["pre_seed"]

[sector]
keywords = ["fintech", "AI", "healthcare", "developer tools"]
unknown = "review"

[roles]
require_open_role = true
locations = ["United States", "Remote"]
"""


def page(data):
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + "</script></html>"


def co(slug, domain, *, id=None, name=None, stage="series_a", employees=50, sectors=("FinTech",),
       locations=("New York, NY, USA",), roles=3, website=None):
    """One company as a board would list it. ``roles`` is its open-role count in the wanted locations."""
    return {"slug": slug, "id": id or abs(hash(slug)) % 100000 + 1, "name": name or slug.replace("-", " ").title(),
            "domain": domain, "websiteUrl": website if website is not None else (f"http://{domain}" if domain else ""),
            "stage": stage, "approxEmployees": employees, "industryTags": list(sectors), "topics": [],
            "locations": [{"id": "1", "name": n} for n in locations], "_roles": roles}


def board_pages(base, name, network_id, listed, locations=LOCATIONS):
    """Every page of a board that the harvest may read."""
    pages = {
        base + "/jobs": page({"props": {"pageProps": {"network": {"id": network_id, "name": name},
                                                      "initialState": {"jobs": {"total": 999, "found": []}}}}}),
        base + "/sitemap.xml": "<urlset>" + "".join(
            f"<url><loc>{base}/companies/{c['slug']}</loc></url>"
            f"<url><loc>{base}/companies/{c['slug']}/jobs/1-some-role</loc></url>" for c in listed) + "</urlset>",
    }
    for c in listed:
        company = {k: v for k, v in c.items() if not k.startswith("_")}
        pages[f"{base}/companies/{c['slug']}"] = page({"props": {"pageProps": {"company": company}}})
        pages[base + jobs_filter_path(str(c["id"]), locations)] = page({"props": {"pageProps": {"initialState": {
            "jobs": {"total": c["_roles"], "found": [{"title": "must never be stored", "id": 1}]}}}}})
    return pages


A = "https://jobs.alpha-fund.example"
B = "https://careers.beta-capital.example"


@pytest.fixture
def cfg():
    return cands.parse_config(CONFIG, "test")


def run(conn, cfg, pages, **kwargs):
    web = FakeWeb(pages)
    summary = portfolio.harvest(conn, cfg, fetcher=Fetcher(session=web), **kwargs)
    return summary, web


def by_domain(conn):
    return {c["domain"]: c for c in cands.list_candidates(conn)}


def company_reads(web):
    return [u for u in web.urls if "/companies/" in u]


# --- the page reader, against the recorded fixture ---------------------------

def test_recorded_board_pages_are_parsed():
    network = portfolio.parse_board_page(page({"props": {"pageProps": FIXTURE["jobs_page"]}}))
    assert network == {"id": "9001", "name": "Example Ventures"}
    info = portfolio.parse_company_page(page({"props": {"pageProps": FIXTURE["company_page"]}}))
    assert (info["name"], info["domain"], info["website"]) == (
        "Acme Robotics", "acme-robotics.example", "http://acme-robotics.example")
    assert (info["stage"], info["employees"], info["platform_id"]) == ("series_d", 263, "7000")
    assert "Artificial Intelligence (AI)" in info["sectors"] and "San Jose, CA, USA" in info["locations"]
    xml = "".join(f"<url><loc>{loc}</loc></url>" for loc in FIXTURE["sitemap_sample"]["locs"])
    assert portfolio.parse_sitemap(xml) == (["acme-robotics", "bolt-health", "cobalt-data"], [])
    assert portfolio.parse_jobs_total(page({"props": {"pageProps": FIXTURE["jobs_page"]}})) == 897
    assert portfolio.parse_company_page("<html>not a board</html>") is None
    assert portfolio.parse_board_page("<html>some other site</html>") is None


def test_tags_given_as_objects_and_odd_pages_do_not_stop_a_harvest(conn, cfg):
    portfolio.add_board(conn, A)
    listed = [co("tagged", "tagged.example", id=1), co("odd", "odd.example", id=2), co("plain", "plain.example", id=3)]
    pages = board_pages(A, "Alpha", "1", listed)
    # Some company pages give topics and tags as objects, not strings.
    tagged = {**{k: v for k, v in listed[0].items() if not k.startswith("_")},
              "industryTags": [{"id": 5, "description": "FinTech"}, "Payments"],
              "topics": [{"id": 9, "name": "B2B"}, {"unexpected": True}]}
    pages[A + "/companies/tagged"] = page({"props": {"pageProps": {"company": tagged}}})
    pages[A + "/companies/odd"] = page({"props": {"pageProps": {"company": {"name": "Odd", "locations": 7}}}})
    summary, _ = run(conn, cfg, pages)
    assert summary.boards[0].error is None and summary.boards[0].finished
    assert (summary.boards[0].read, summary.boards[0].unreadable) == (2, 1)
    assert by_domain(conn)["tagged.example"]["sectors"] == ["FinTech", "Payments", "B2B"]
    assert set(by_domain(conn)) == {"tagged.example", "plain.example"}


def test_a_page_that_failed_temporarily_is_tried_again_next_time(conn, cfg):
    portfolio.add_board(conn, A)
    listed = [co("flaky", "flaky.example", id=1), co("gone", "gone.example", id=2)]
    pages = board_pages(A, "Alpha", "1", listed)
    broken = {**pages, A + "/companies/flaky": 503, A + "/companies/gone": 404}
    summary, _ = run(conn, cfg, broken)
    assert summary.boards[0].unreadable == 2 and cands.list_candidates(conn) == []
    _, web = run(conn, cfg, pages)
    assert company_reads(web) == [A + "/companies/flaky"]        # the 404 is remembered, the 503 is not
    assert set(by_domain(conn)) == {"flaky.example"}


# --- registry ----------------------------------------------------------------

def test_boards_are_registered_listed_and_removed(conn, capsys):
    assert cli.main(["portfolio", "add", A + "/jobs?page=2", "--name", "Alpha"]) == 0
    assert cli.main(["portfolio", "add", "careers.beta-capital.example/companies"]) == 0
    assert [(b["url"], b["name"]) for b in portfolio.list_boards(conn)] == [(A, "Alpha"), (B, "")]
    assert cli.main(["portfolio", "add", A]) == 1
    assert "already registered" in capsys.readouterr().err
    assert cli.main(["portfolio", "add", "not a url"]) == 1
    capsys.readouterr()
    assert cli.main(["portfolio", "list"]) == 0
    out = capsys.readouterr().out
    assert A in out and "2 boards." in out
    assert cli.main(["portfolio", "remove", "Alpha"]) == 0
    assert cli.main(["portfolio", "remove", "2"]) == 0
    assert portfolio.list_boards(conn) == []
    assert cli.main(["portfolio", "remove", "Alpha"]) == 1


# --- identity ----------------------------------------------------------------

def test_one_company_on_two_boards_is_one_candidate(conn, cfg):
    portfolio.add_board(conn, A, "Alpha Fund")
    portfolio.add_board(conn, B)
    shared = dict(domain="acme.example", id=77, employees=120)
    pages = {
        **board_pages(A, "ignored: a name was given", "1", [co("acme", **shared), co("only-alpha", "only-alpha.example")]),
        # Same company under another slug and a www. website; the domain is the identity.
        **board_pages(B, "Beta Capital", "2", [co("acme-inc", website="https://www.acme.example/about", **shared),
                                               co("only-beta", "only-beta.example")]),
    }
    summary, _ = run(conn, cfg, pages)
    assert [(b.name, b.listed, b.read) for b in summary.boards] == [("Alpha Fund", 2, 2), ("Beta Capital", 2, 2)]
    found = by_domain(conn)
    assert set(found) == {"acme.example", "only-alpha.example", "only-beta.example"}
    acme = found["acme.example"]
    assert [b["name"] for b in acme["boards"]] == ["Alpha Fund", "Beta Capital"]     # every board that lists it
    assert [b["slug"] for b in acme["boards"]] == ["acme", "acme-inc"]
    assert (acme["name"], acme["stage"], acme["employees"], acme["sectors"], acme["locations"]) == (
        "Acme Inc", "series_a", 120, ["FinTech"], ["New York, NY, USA"])
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 3
    assert cands.list_candidates(conn)[0]["domain"] == "acme.example"              # most boards first
    # Removing a board keeps the candidate and its other listing.
    portfolio.remove_board(conn, A)
    assert [b["name"] for b in by_domain(conn)["acme.example"]["boards"]] == ["Beta Capital"]


def test_company_with_no_domain_is_skipped_and_not_re_read(conn, cfg):
    portfolio.add_board(conn, A)
    pages = board_pages(A, "Alpha", "1", [co("mystery", "", website=""), co("acme", "acme.example")])
    summary, web = run(conn, cfg, pages)
    assert (summary.boards[0].read, summary.boards[0].no_domain) == (1, 1)
    assert set(by_domain(conn)) == {"acme.example"}
    _, web = run(conn, cfg, pages)
    assert company_reads(web) == []


# --- the rules ---------------------------------------------------------------

def verdict(cfg, **fields):
    base = {"employees": 50, "stage": "series_a", "sectors": ["FinTech"],
            "matching_roles": 2, "roles_rule": cfg.roles_rule}
    return cands.evaluate({**base, **fields}, cfg)


def test_size_rule(cfg):
    assert verdict(cfg, employees=10).status == "eligible"
    small = verdict(cfg, employees=9)
    assert (small.status, small.reason) == ("rejected", "9 employees, below the minimum of 10")
    unknown = verdict(cfg, employees=None)
    assert (unknown.status, unknown.reason) == ("review", "the board gives no employee count")
    assert verdict(cands.parse_config(CONFIG.replace('unknown = "review"', 'unknown = "pass"', 1)),
                   employees=None).status == "eligible"


def test_stage_rule(cfg):
    for stage in ("seed", "series_b", "SERIES_A", "ipo"):
        assert verdict(cfg, stage=stage).status == "eligible", stage
    pre = verdict(cfg, stage="pre_seed")
    assert (pre.status, pre.reason) == ("rejected", "stage pre_seed is one you reject")
    for stage, said in (("other", "'other'"), (None, "not given"), ("angel", "'angel'")):
        v = verdict(cfg, stage=stage)
        assert v.status == "review" and v.reason == f"stage is {said}, neither accepted nor rejected by the config"


def test_sector_rule(cfg):
    for tags, keyword in ((["FinTech"], "fintech"), (["Retail", "Artificial Intelligence (AI)"], "AI"),
                          (["Health Care"], "healthcare"), (["Developer Tools & APIs"], "developer tools")):
        v = verdict(cfg, sectors=tags)
        assert v.status == "eligible" and f'matches "{keyword}"' in v.checks["sector"]["detail"], tags
    # "AI" must not match inside other words.
    miss = verdict(cfg, sectors=["Retail", "Aviation", "Hospitality"])
    assert miss.status == "rejected" and miss.reason.startswith("none of its sectors match your keywords (Retail")
    none = verdict(cfg, sectors=[])
    assert (none.status, none.reason) == ("review", "the board gives no sector tags")


def test_open_role_rule_and_rule_order(cfg):
    ok = verdict(cfg, matching_roles=1)
    assert ok.status == "eligible" and ok.checks["roles"]["detail"] == "1 open role in United States or Remote"
    zero = verdict(cfg, matching_roles=0)
    assert (zero.status, zero.reason) == ("rejected", "no open role in United States or Remote")
    pending = verdict(cfg, matching_roles=None)
    assert pending.status == "review" and pending.needs_role_check and pending.checks["roles"]["result"] == "pending"
    # A count taken for other locations does not answer today's rule.
    assert verdict(cfg, matching_roles=4, roles_rule='["Canada"]').needs_role_check
    # The first failing rule decides, and the costly rule is not even asked for.
    v = verdict(cfg, employees=3, stage="pre_seed", sectors=["Retail"], matching_roles=None)
    assert v.reason == "9 employees".replace("9", "3") + ", below the minimum of 10"
    assert v.checks["roles"]["result"] == "skipped" and not v.needs_role_check
    # Several undecided rules are all named.
    both = verdict(cfg, employees=None, stage="other")
    assert both.status == "review" and "employee count" in both.reason and "stage is 'other'" in both.reason
    assert not both.needs_role_check


def test_role_check_is_asked_last_and_only_for_candidates_still_in_the_running(conn, cfg):
    portfolio.add_board(conn, A)
    listed = [co("good", "good.example", id=1, roles=4), co("no-us-roles", "no-us-roles.example", id=2, roles=0),
              co("tiny", "tiny.example", id=3, employees=4), co("preseed", "preseed.example", id=4, stage="pre_seed"),
              co("off-sector", "off-sector.example", id=5, sectors=("Retail",)),
              co("unknown-stage", "unknown-stage.example", id=6, stage="other")]
    summary, web = run(conn, cfg, board_pages(A, "Alpha", "1", listed))
    role_requests = [u for u in web.urls if "/jobs?filter=" in u]
    assert role_requests == [A + jobs_filter_path("1", LOCATIONS), A + jobs_filter_path("2", LOCATIONS)]
    assert web.urls.index(role_requests[0]) > max(web.urls.index(u) for u in company_reads(web))   # last
    assert summary.boards[0].role_checks == 2
    states = {d: (c["state"], c["reason"]) for d, c in by_domain(conn).items()}
    assert states == {
        "good.example": ("eligible", "passes every rule"),
        "no-us-roles.example": ("rejected", "no open role in United States or Remote"),
        "tiny.example": ("rejected", "4 employees, below the minimum of 10"),
        "preseed.example": ("rejected", "stage pre_seed is one you reject"),
        "off-sector.example": ("rejected", "none of its sectors match your keywords (Retail)"),
        "unknown-stage.example": ("review", "stage is 'other', neither accepted nor rejected by the config"),
    }
    assert by_domain(conn)["good.example"]["matching_roles"] == 4
    assert cands.funnel(conn) == {
        "listed": 6, "already_tracked": 0, "dismissed": 0, "considered": 6, "passed_size": 5, "passed_stage": 3,
        "passed_sector": 2, "passed_roles": 1, "eligible": 1, "needs_review": 1, "awaiting_role_check": 0,
        "rejected": 4}


def test_no_role_is_ever_stored_from_a_board(conn, cfg):
    portfolio.add_board(conn, A)
    run(conn, cfg, board_pages(A, "Alpha", "1", [co("good", "good.example", roles=7)]))
    assert conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM role_descriptions").fetchone()[0] == 0
    dump = "\n".join(conn.iterdump())
    assert "must never be stored" not in dump          # the role on the counted page left no trace
    assert by_domain(conn)["good.example"]["matching_roles"] == 7


# --- re-applying the filter --------------------------------------------------

def test_config_change_is_re_applied_without_the_network(conn, cfg, capsys, isolated_db_path):
    portfolio.add_board(conn, A)
    listed = [co("big", "big.example", employees=400, roles=2), co("mid", "mid.example", employees=40, roles=2),
              co("retail", "retail.example", employees=500, sectors=("Retail",), roles=5)]
    run(conn, cfg, board_pages(A, "Alpha", "1", listed))
    assert {d: c["state"] for d, c in by_domain(conn).items()} == {
        "big.example": "eligible", "mid.example": "eligible", "retail.example": "rejected"}

    config = isolated_db_path.parent / "candidates.toml"
    config.write_text(CONFIG.replace("min_employees = 10", "min_employees = 100")
                      .replace('"developer tools"]', '"developer tools", "retail"]'))
    assert cli.main(["candidates", "apply"]) == 0        # conftest blocks sockets: this proves no network
    out = capsys.readouterr().out
    assert "Filter applied (no network)." in out
    found = by_domain(conn)
    assert (found["mid.example"]["state"], found["mid.example"]["reason"]) == (
        "rejected", "40 employees, below the minimum of 100")
    assert found["big.example"]["state"] == "eligible"                  # its stored role count still answers
    # Newly passing the cheap rules: its roles were never counted, so it waits for a harvest.
    retail = found["retail.example"]
    assert retail["state"] == "review" and retail["checks"]["roles"]["result"] == "pending"
    assert "1 passed the other rules and still need their open roles counted" in out
    assert cands.funnel(conn)["awaiting_role_check"] == 1


# --- linking to tracked companies --------------------------------------------

def test_candidate_matching_a_tracked_company_is_linked_never_duplicated(conn, cfg, capsys):
    companies.add_company(conn, "Acme Corporation", website="https://www.acme.example/home", tier=1)
    portfolio.add_board(conn, A)
    pages = board_pages(A, "Alpha", "1", [co("acme", "acme.example", id=1), co("other", "other.example", id=2)])
    _, web = run(conn, cfg, pages)
    acme = by_domain(conn)["acme.example"]
    assert (acme["state"], acme["tracked_as"]) == ("tracked", "Acme Corporation")
    assert A + jobs_filter_path("1", LOCATIONS) not in web.urls           # no role check spent on it
    assert [c["domain"] for c in cands.list_candidates(conn, "tracked")] == ["acme.example"]
    assert cands.funnel(conn)["already_tracked"] == 1 and cands.funnel(conn)["considered"] == 1
    with pytest.raises(cands.CandidateError, match="already tracked as 'Acme Corporation'"):
        cands.activate(conn, acme)
    assert cli.main(["candidates", "activate", "--all"]) == 0
    capsys.readouterr()
    assert sorted(c["name"] for c in companies.list_companies(conn)) == ["Acme Corporation", "Other"]

    # A company added by hand later is linked the next time the filter is applied.
    portfolio.add_board(conn, B)
    run(conn, cfg, board_pages(B, "Beta", "2", [co("late", "late.example", id=3)]))
    companies.add_company(conn, "Late Co", website="late.example")
    cands.apply_filter(conn, cfg)
    assert by_domain(conn)["late.example"]["state"] == "tracked"


# --- activation, dismissal ---------------------------------------------------

@pytest.fixture
def harvested(conn, cfg, isolated_db_path):
    (isolated_db_path.parent / "candidates.toml").write_text(CONFIG)
    portfolio.add_board(conn, A, "Alpha Fund")
    portfolio.add_board(conn, B)
    shared = dict(domain="acme.example", id=1, employees=300)
    pages = {
        **board_pages(A, "x", "1", [co("acme", **shared), co("bolt", "bolt.example", id=2, employees=80),
                                    co("cobalt", "cobalt.example", id=3, employees=20),
                                    co("tiny", "tiny.example", id=4, employees=2),
                                    co("vague", "vague.example", id=5, stage="other")]),
        **board_pages(B, "Beta Capital", "2", [co("acme", **shared)]),
    }
    _, web = run(conn, cfg, pages)
    return conn, pages


def test_activation_adds_tier_3_companies_tagged_with_their_boards(harvested, capsys):
    conn, _ = harvested
    assert [c["name"] for c in cands.list_candidates(conn, "eligible")] == ["Acme", "Bolt", "Cobalt"]
    assert cli.main(["candidates", "activate"]) == 1                                   # must say which
    capsys.readouterr()

    assert cli.main(["candidates", "activate", "--first", "1"]) == 0
    out = capsys.readouterr().out
    assert "Activated Acme (acme.example) as a tier 3 company, tagged portfolio:alpha-fund, portfolio:beta-capital." in out
    assert "find them with: rolescout companies resolve" in out
    acme = next(c for c in companies.list_companies(conn) if c["name"] == "Acme")
    assert (acme["tier"], acme["tags"], acme["active"], acme["website"], acme["boards"]) == (
        3, ["portfolio:alpha-fund", "portfolio:beta-capital"], True, "http://acme.example", [])   # no board mapped
    assert cands.get_candidate(conn, "acme.example")["state"] == "tracked"

    assert cli.main(["candidates", "activate", "bolt.example"]) == 0                   # by domain
    assert cli.main(["candidates", "activate", "Vague"]) == 0                          # in review: your call, by name
    capsys.readouterr()
    assert cli.main(["candidates", "activate", "Tiny"]) == 1                           # rejected: needs --force
    assert "is rejected (2 employees, below the minimum of 10); pass --force" in capsys.readouterr().err
    assert cli.main(["candidates", "activate", "Tiny", "--force"]) == 0
    assert cli.main(["candidates", "activate", "--all"]) == 0                          # the one eligible left
    capsys.readouterr()
    assert sorted(c["name"] for c in companies.list_companies(conn)) == ["Acme", "Bolt", "Cobalt", "Tiny", "Vague"]
    assert cli.main(["candidates", "activate", "--all"]) == 0
    assert "No eligible candidates to activate." in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 0              # mapping is resolve's job
    assert conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 0


def test_dismissed_candidates_stay_dismissed_and_can_be_restored(harvested, cfg, capsys):
    conn, pages = harvested
    assert cli.main(["candidates", "dismiss", "Bolt", "--note", "not for me"]) == 0
    assert "will not be suggested again" in capsys.readouterr().out
    bolt = cands.get_candidate(conn, "Bolt")
    assert (bolt["state"], bolt["dismiss_note"]) == ("dismissed", "not for me")
    assert "Bolt" not in [c["name"] for c in cands.list_candidates(conn, "eligible")]
    run(conn, cfg, pages, recheck_days=0)                                # a full re-read changes nothing
    assert cands.get_candidate(conn, "Bolt")["state"] == "dismissed"
    assert cli.main(["candidates", "activate", "--all"]) == 0
    capsys.readouterr()
    assert "Bolt" not in [c["name"] for c in companies.list_companies(conn)]
    assert cli.main(["candidates", "restore", "bolt.example"]) == 0
    assert cands.get_candidate(conn, "Bolt")["state"] == "eligible"
    assert cli.main(["candidates", "restore", "Bolt"]) == 0
    assert "was not dismissed" in capsys.readouterr().out
    assert cli.main(["candidates", "dismiss", "Nope"]) == 1


def test_list_and_why_commands(harvested, capsys):
    conn, _ = harvested
    assert cli.main(["candidates", "--status", "rejected"]) == 0
    out = capsys.readouterr().out
    assert "tiny.example" in out and "2 employees, below the minimum of 10" in out and "1 candidate with status rejected" in out
    assert cli.main(["candidates", "--json", "--limit", "1"]) == 0
    assert [c["domain"] for c in json.loads(capsys.readouterr().out)] == ["acme.example"]

    assert cli.main(["candidates", "why", "acme"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Acme  (acme.example)\n")
    assert "Listed on: Alpha Fund, Beta Capital" in out and "Status: eligible: passes every rule" in out
    for line in ("Size:       pass     300 employees (minimum 10)", "Stage:      pass     stage series_a",
                 'Sector:     pass     sector "FinTech" matches "fintech"',
                 "Open roles: pass     3 open roles in United States or Remote"):
        assert line in out
    assert cli.main(["candidates", "why", "vague.example"]) == 0
    out = capsys.readouterr().out
    assert "Status: needs review: stage is 'other'" in out and "Open roles: skipped" in out
    assert cli.main(["candidates", "why", "Nope"]) == 1
    assert "no candidate named 'Nope'" in capsys.readouterr().err


# --- polite, bounded, resumable ----------------------------------------------

def test_api_host_is_never_requested(conn, cfg):
    fetcher = Fetcher(session=FakeWeb({}))
    with pytest.raises(portfolio.Forbidden, match="only pages on https://jobs.alpha-fund.example are read"):
        fetcher.get(A, "https://api.getro.com/api/v2/collections/1/search/jobs")
    assert fetcher.session.urls == []                                  # refused before any request
    portfolio.add_board(conn, A)
    _, web = run(conn, cfg, board_pages(A, "Alpha", "1", [co("acme", "acme.example")]))
    assert {u.split("/")[2] for u in web.urls} == {"jobs.alpha-fund.example"}


def test_robots_txt_is_honored_for_each_url(conn, cfg):
    portfolio.add_board(conn, A)
    pages = board_pages(A, "Alpha", "1", [co("open", "open.example", id=1), co("closed", "closed.example", id=2)])
    pages[A + "/robots.txt"] = "User-agent: *\nDisallow: /companies/closed\nDisallow: /jobs?filter=\n"
    summary, web = run(conn, cfg, pages)
    assert A + "/companies/closed" not in web.urls and A + "/companies/open" in web.urls
    assert not [u for u in web.urls if "/jobs?filter=" in u]            # disallowed: not asked
    assert web.urls.count(A + "/robots.txt") == 1
    assert summary.boards[0].unreadable == 1
    found = by_domain(conn)
    assert set(found) == {"open.example"}
    assert found["open.example"]["checks"]["roles"]["result"] == "pending"   # could not be counted: stays pending

    portfolio.add_board(conn, B)
    summary, web = run(conn, cfg, {B + "/robots.txt": "User-agent: *\nDisallow: /\n"}, boards=[B])
    assert summary.boards[0].error == "robots.txt disallows reading this board"
    assert [u for u in web.urls if u.startswith(B)] == [B + "/robots.txt"]      # nothing else on that board


def test_a_site_that_is_not_a_getro_board_is_reported(conn, cfg):
    portfolio.add_board(conn, A)
    summary, _ = run(conn, cfg, {A + "/jobs": "<html>some other job site</html>"})
    assert summary.boards[0].error == "not a Getro board; only Getro boards can be harvested"
    assert portfolio.list_boards(conn)[0]["last_error"] == summary.boards[0].error
    assert cands.list_candidates(conn) == []


def test_time_limit_stops_the_harvest_and_the_next_one_continues(conn, cfg, monkeypatch):
    portfolio.add_board(conn, A)
    listed = [co(f"c{i}", f"c{i}.example", id=i + 1) for i in range(6)]
    pages = board_pages(A, "Alpha", "1", listed)
    clock = [0.0]
    monkeypatch.setattr(portfolio.time, "monotonic", lambda: clock[0])

    class Ticking(FakeWeb):
        def get(self, url, **kwargs):
            clock[0] += 10                     # every request "takes" ten seconds
            return super().get(url, **kwargs)

    def harvest_once(limit):
        web = Ticking(pages)
        return portfolio.harvest(conn, cfg, fetcher=Fetcher(session=web), time_limit=limit), web

    first, web = harvest_once(65)              # robots, /jobs, sitemap, then time for three companies
    assert first.stopped_early and not first.complete and len(company_reads(web)) == 4
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 4      # what was read is kept
    assert first.requests == len(web.urls)

    clock[0] = 0.0
    second, web = harvest_once(None)
    # Only the two companies not read yet, then the role checks for all six.
    assert sorted(company_reads(web)) == [A + "/companies/c4", A + "/companies/c5"]
    assert second.complete and conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 6
    assert len(cands.list_candidates(conn, "eligible")) == 6


def test_repeat_harvest_reads_only_new_and_stale_companies(conn, cfg):
    portfolio.add_board(conn, A)
    old = [co("a", "a.example", id=1), co("b", "b.example", id=2), co("gone", "gone.example", id=3)]
    run(conn, cfg, board_pages(A, "Alpha", "1", old))
    new = [old[0], old[1], co("fresh", "fresh.example", id=4)]
    summary, web = run(conn, cfg, board_pages(A, "Alpha", "1", new))
    assert company_reads(web) == [A + "/companies/fresh"]
    assert (summary.boards[0].listed, summary.boards[0].read, summary.boards[0].fresh) == (3, 1, 2)
    assert [u for u in web.urls if "/jobs?filter=" in u] == [A + jobs_filter_path("4", LOCATIONS)]
    # No longer on the board: the listing goes, the candidate stays.
    assert by_domain(conn)["gone.example"]["boards"] == []

    # One went stale: only it is read again.
    conn.execute("UPDATE candidates SET last_checked_at = '2020-01-01T00:00:00Z' WHERE domain = 'b.example'")
    _, web = run(conn, cfg, board_pages(A, "Alpha", "1", new))
    assert company_reads(web) == [A + "/companies/b"]


# --- the daily job -----------------------------------------------------------

def daily(monkeypatch, conn, pages, *argv):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: scout.run_scout(
        c, adapter_for=Scripted(tracked=[[role("T1")]] * 5), on_board=kw.get("on_board")))
    web = FakeWeb(pages)
    real = Fetcher
    monkeypatch.setattr(portfolio, "Fetcher", lambda delay=1.0: real(session=web))
    return cli.main(["daily", "--recheck-days", "0", *argv]), web


def test_daily_job_harvests_weekly_after_scout_and_backup(conn, monkeypatch, capsys, tmp_path, isolated_db_path):
    (isolated_db_path.parent / "candidates.toml").write_text(CONFIG)
    companies.add_company(conn, "Tracked")
    companies.add_board(conn, "Tracked", "greenhouse", "tracked")
    portfolio.add_board(conn, A)
    pages = board_pages(A, "Alpha", "1", [co("acme", "acme.example", id=1)])

    code, web = daily(monkeypatch, conn, pages, "--backup-dir", str(tmp_path / "b"))
    out = capsys.readouterr().out
    assert code == 0 and out.index("Run 1 completed") < out.index("Backup written") < out.index("Weekly portfolio harvest")
    assert "Eligible:                        1" in out
    assert db.get_meta(conn, portfolio.HARVEST_META_KEY) is not None

    code, web = daily(monkeypatch, conn, pages)             # the next day: not due
    assert code == 0 and web.urls == [] and "Weekly portfolio harvest" not in capsys.readouterr().out
    db.set_meta(conn, portfolio.HARVEST_META_KEY, "2020-01-01T00:00:00Z")
    code, web = daily(monkeypatch, conn, pages)             # a week later
    assert "Weekly portfolio harvest" in capsys.readouterr().out and web.urls
    code, web = daily(monkeypatch, conn, pages, "--harvest-days", "0")
    assert web.urls == []                                   # turned off


def test_cut_short_or_failing_harvest_never_affects_the_scout(conn, monkeypatch, capsys, isolated_db_path):
    (isolated_db_path.parent / "candidates.toml").write_text(CONFIG)
    companies.add_company(conn, "Tracked")
    companies.add_board(conn, "Tracked", "greenhouse", "tracked")
    portfolio.add_board(conn, A)
    pages = board_pages(A, "Alpha", "1", [co(f"c{i}", f"c{i}.example", id=i + 1) for i in range(3)])

    # Never monkeypatch.undo() in this file: it would also drop the database isolation.
    clock = [0.0]
    real_monotonic, real_get = portfolio.time.monotonic, FakeWeb.get
    monkeypatch.setattr(portfolio.time, "monotonic", lambda: clock[0])

    def slow_get(self, url, **kwargs):
        clock[0] += 400
        return real_get(self, url, **kwargs)
    monkeypatch.setattr(FakeWeb, "get", slow_get)
    code, web = daily(monkeypatch, conn, pages, "--harvest-minutes", "20")
    out = capsys.readouterr().out
    assert code == 0 and ", ok ===" in out and "Run 1 completed" in out
    assert "stopped at the time limit, the next harvest continues from here" in out
    assert db.get_meta(conn, portfolio.HARVEST_META_KEY) is None        # not done: tomorrow carries on
    monkeypatch.setattr(portfolio.time, "monotonic", real_monotonic)
    monkeypatch.setattr(FakeWeb, "get", real_get)

    real_harvest = portfolio.harvest
    monkeypatch.setattr(portfolio, "harvest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: scout.run_scout(
        c, adapter_for=Scripted(tracked=[[role("T1")]])))
    assert cli.main(["daily", "--recheck-days", "0"]) == 0
    out = capsys.readouterr().out
    assert "Portfolio harvest failed, will try again tomorrow: RuntimeError: boom" in out and ", ok ===" in out

    # No config yet: also only a message.
    (isolated_db_path.parent / "candidates.toml").unlink()
    monkeypatch.setattr(portfolio, "harvest", real_harvest)
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: scout.run_scout(
        c, adapter_for=Scripted(tracked=[[role("T1")]])))
    assert cli.main(["daily", "--recheck-days", "0"]) == 0
    assert "rolescout candidates init" in capsys.readouterr().out


# --- config, starter, upgrade ------------------------------------------------

def test_init_seeds_sector_keywords_from_the_repo_filters(conn, capsys, isolated_db_path, monkeypatch):
    monkeypatch.chdir(REPO)
    path = isolated_db_path.parent / "candidates.toml"
    assert cli.main(["candidates", "init"]) == 0
    assert f"Wrote {path}: 14 sector keywords from role_filters.csv." in capsys.readouterr().out
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = cands.load_config(path)
    assert (loaded.min_employees, loaded.size_unknown, loaded.stage_reject) == (10, "review", ["pre_seed"])
    assert "seed" in loaded.stage_accept and "pre_seed" not in loaded.stage_accept
    assert loaded.sector_keywords[:3] == ["fintech", "media", "adtech"]
    assert (loaded.require_open_role, loaded.role_locations) == (True, ["United States", "Remote"])
    assert cli.main(["candidates", "init"]) == 1
    assert "already exists" in capsys.readouterr().err

    monkeypatch.chdir(isolated_db_path.parent)
    assert cli.main(["candidates", "init", "--force"]) == 0
    assert "example sector keywords only" in capsys.readouterr().out
    assert "EXAMPLES ONLY" in path.read_text()


def test_bad_configs_are_refused(conn, capsys, isolated_db_path):
    path = isolated_db_path.parent / "candidates.toml"
    assert cli.main(["candidates", "apply"]) == 1
    assert "Create one with: rolescout candidates init" in capsys.readouterr().err
    for text, message in (("[size", "is not valid TOML"), ("[sise]\n", "unknown section sise"),
                          ("[size]\nmin = 3\n", "unknown setting min in [size]"),
                          ('[size]\nunknown = "maybe"\n', "size.unknown must be one of"),
                          ('[stage]\naccept = ["seed"]\nreject = ["Seed"]\n', "in both accept and reject"),
                          ("[roles]\nrequire_open_role = true\n", "must name at least one location")):
        path.write_text(text)
        assert cli.main(["candidates", "apply"]) == 1
        assert message in capsys.readouterr().err


def test_version_6_database_upgrades_in_place(tmp_path, monkeypatch, cfg):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", 6)
    old = db.connect(path)
    assert db.schema_version(old) == 6
    old.execute("INSERT INTO companies (name, website, created_at) VALUES ('Acme', 'https://acme.example', 'x')")
    old.close()
    monkeypatch.undo()
    conn = db.connect(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION >= 7
    portfolio.add_board(conn, A)
    run(conn, cfg, board_pages(A, "Alpha", "1", [co("acme", "acme.example"), co("bolt", "bolt.example")]))
    assert {d: c["state"] for d, c in by_domain(conn).items()} == {
        "acme.example": "tracked", "bolt.example": "eligible"}
