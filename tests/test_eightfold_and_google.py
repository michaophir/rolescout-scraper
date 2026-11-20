"""The Eightfold adapter (both list endpoints, pacing, 429s, resolve and gaps)
and the adapter for Google's own career site, against recorded responses."""

from pathlib import Path

import pytest

from rolescout import cli, companies, gaps, scout
from rolescout.adapters import ADAPTERS, BoardNotFound, SourceError, eightfold, google
from rolescout.adapters.eightfold import EightfoldAdapter
from rolescout.adapters.google import FEED_URL, GoogleAdapter
from rolescout.resolve import Resolver, recheck_targets, resolve_companies, scan_for_boards
from tests.conftest import FakeWeb, FakeWebResponse
from tests.test_job_systems import record_unsupported, resolve_one

JOBS = "https://{}/api/apply/v2/jobs?domain={}&start={}&num=10&sort_by=timestamp"
SEARCH = "https://{}/api/pcsx/search?domain={}&start={}&query=&location=&sort_by=timestamp"
SITE, OWN_HOST = "acme.eightfold.ai", "careers.globex.example"
FEED_FIXTURE = Path(__file__).parent / "fixtures" / "google_feed_roles_from_several_employers.xml"


@pytest.fixture(autouse=True)
def no_real_pauses(monkeypatch):
    """Adapters built by resolve and the CLI would otherwise really wait."""
    monkeypatch.setattr(eightfold, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(EightfoldAdapter, "_sleep", lambda self, seconds: None, raising=False)


class Flaky(FakeWeb):
    """A FakeWeb where a URL can answer a list of things in turn (the last one repeats).
    An exception in the list is raised, as a dropped connection would be."""

    def get(self, url, **kwargs):
        body = self.pages.get(url)
        if isinstance(body, list):
            self.urls.append(url)
            answer = body.pop(0) if len(body) > 1 else body[0]
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, tuple):                       # (status, headers)
                resp = FakeWebResponse(url, None, answer[0])
                resp.headers = answer[1]
                return resp
            return FakeWebResponse(url, None, answer) if isinstance(answer, int) else FakeWebResponse(url, answer, 200)
        return super().get(url, **kwargs)


def jobs_board(load_fixture):
    pages = load_fixture("eightfold_board_read_in_overlapping_pages.json")
    return {JOBS.format(SITE, "acme.com", 0): pages["start=0"], JOBS.format(SITE, "acme.com", 8): pages["start=8"]}


def search_board(load_fixture):
    return {JOBS.format(OWN_HOST, "globex.example", 0): 403,
            SEARCH.format(OWN_HOST, "globex.example", 0): load_fixture(
                "eightfold_search_endpoint_names_fields_differently.json")}


def quiet(web, **kwargs):
    waits = []
    adapter = EightfoldAdapter(web, min_interval=kwargs.pop("min_interval", 0), sleep=waits.append, **kwargs)
    return adapter, waits


# --- eightfold: references -----------------------------------------------------

@pytest.mark.parametrize("url, ref", [
    ("https://acme.eightfold.ai/careers?domain=acme.com", "acme.eightfold.ai/acme.com"),
    ("https://acme.eightfold.ai/careers/job/700000000001?domain=acme.com&hl=en", "acme.eightfold.ai/acme.com"),
    ("https://Acme.Eightfold.ai/careers", "acme.eightfold.ai"),
    ("acme.eightfold.ai/careers?query=product&domain=acme.com", "acme.eightfold.ai/acme.com"),
    ("https://careers.globex.example/careers?domain=globex.example", "careers.globex.example/globex.example"),
    ("https://careers.globex.example/api/pcsx/search?domain=globex.example&start=0",
     "careers.globex.example/globex.example"),
    ("https://acme.eightfold.ai/api/apply/v2/jobs?domain=acme.com&start=10", "acme.eightfold.ai/acme.com"),
])
def test_eightfold_reference_from_every_url_shape(url, ref):
    assert eightfold.parse_eightfold_url(url) == ref
    assert EightfoldAdapter.canonical_ref(url) == ref
    assert EightfoldAdapter.canonical_ref(ref) == ref
    assert EightfoldAdapter.canonical_ref(ref.upper() + "/") == ref


@pytest.mark.parametrize("text", [
    "https://www.eightfold.ai/customers/", "https://eightfold.ai/", "https://careers.globex.example/careers",
    "https://careers.globex.example/about?domain=globex.example", "acme", "https://localhost/careers?domain=acme.com",
    "https://10.0.0.1/careers?domain=acme.com", "",
])
def test_text_that_is_not_an_eightfold_site_is_refused(text):
    assert eightfold.parse_eightfold_url(text) is None
    with pytest.raises(ValueError, match="is not an Eightfold career site"):
        EightfoldAdapter.canonical_ref(text)


def test_eightfold_boards_are_found_in_page_text_and_are_no_longer_unsupported():
    page = '''<a href="https://acme.eightfold.ai/careers">Jobs</a>
        <a href="https://acme.eightfold.ai/careers/job/7?domain=acme.com">A role</a>
        <a href="https://careers.globex.example/careers?pid=9&domain=globex.example">Other</a>
        <a href="https://www.eightfold.ai/">Powered by</a> <a href="https://shop.example/careers">no domain</a>'''
    assert eightfold.find_refs(page) == ["acme.eightfold.ai/acme.com", "careers.globex.example/globex.example"]
    supported, unsupported = scan_for_boards(page)
    assert set(supported) == {("eightfold", "acme.eightfold.ai/acme.com"),
                              ("eightfold", "careers.globex.example/globex.example")}
    assert "eightfold" not in unsupported and "eightfold" in ADAPTERS and not ADAPTERS["eightfold"].guessable


# --- eightfold: fetching -------------------------------------------------------

def test_eightfold_jobs_endpoint_is_read_in_overlapping_pages(load_fixture):
    web = FakeWeb(jobs_board(load_fixture))
    adapter, _ = quiet(web)
    roles = adapter.fetch("https://acme.eightfold.ai/careers?domain=acme.com", company="Acme")
    assert len(roles) == 13 and len({r.source_job_id for r in roles}) == 13
    first, remote = roles[0], roles[2]
    assert (first.source, first.source_job_id, first.company) == ("eightfold", "700000000001", "Acme")
    assert (first.title, first.department) == ("AI Engineer 6 - AI Foundation & Tooling, Ads Platform", "Data & Insights")
    assert (first.location, first.workplace_type) == ("Los Gatos, California, United States of America", "onsite")
    assert (first.url, first.date_posted) == ("https://acme.eightfold.ai/careers/job/700000000001", "2024-07-23")
    assert first.description == ""                                   # metadata only
    assert (remote.location, remote.workplace_type) == ("Remote, United States", "remote")
    assert roles[10].location == ("Los Gatos, California, United States of America; "
                                  "New York, New York, United States of America")
    assert web.urls == ["https://acme.eightfold.ai/robots.txt", JOBS.format(SITE, "acme.com", 0),
                        JOBS.format(SITE, "acme.com", 8)]
    assert (adapter.last_stats.requests, adapter.last_stats.complete) == (3, True)
    # Page two starts two roles before the end of page one, so a list that moved by one loses nothing.
    assert [p["id"] for p in web.pages[JOBS.format(SITE, "acme.com", 8)]["positions"][:2]] == [
        p["id"] for p in web.pages[JOBS.format(SITE, "acme.com", 0)]["positions"][8:]]
    assert adapter.confirm("acme.eightfold.ai/acme.com") == ("acme.eightfold.ai/acme.com", 13)


def test_eightfold_search_endpoint_is_used_when_the_site_refuses_the_other(load_fixture):
    web = FakeWeb(search_board(load_fixture))
    adapter, _ = quiet(web)
    roles = adapter.fetch("careers.globex.example/globex.example")
    assert [r.title for r in roles] == ["Senior Applied Scientist", "Principal Product Manager",
                                        "Principal Product Manager", "Digital Account Manager Swedish Speaking"]
    first = roles[0]
    assert (first.source_job_id, first.company, first.department) == (
        "1900000000000001", "globex.example", "Applied Sciences")
    assert first.location == "Redmond, WA, US; New York, NY, US; Mountain View, CA, US"
    assert (first.url, first.date_posted, first.workplace_type) == (
        "https://careers.globex.example/careers/job/1900000000000001", "2026-11-18", "onsite")
    assert web.urls[1:] == [JOBS.format(OWN_HOST, "globex.example", 0), SEARCH.format(OWN_HOST, "globex.example", 0)]
    assert adapter.last_stats.complete


def test_a_reference_without_the_domain_reads_it_from_the_careers_page(load_fixture):
    web = FakeWeb({**jobs_board(load_fixture), "https://acme.eightfold.ai/careers":
                   '<link href="/careers?domain=acme.com"><a href="/careers/job/7?hl=en&domain=acme.com">x</a>'})
    adapter, _ = quiet(web)
    assert adapter.confirm("https://acme.eightfold.ai/careers") == ("acme.eightfold.ai/acme.com", 13)
    assert len(adapter.fetch("acme.eightfold.ai")) == 13
    silent = FakeWeb({"https://acme.eightfold.ai/careers": "<html>nothing here</html>"})
    with pytest.raises(SourceError, match="does not name the company's domain"):
        quiet(silent)[0].fetch("acme.eightfold.ai")


def test_a_missing_or_disallowed_site_fails_cleanly(load_fixture):
    with pytest.raises(BoardNotFound):
        quiet(FakeWeb({}))[0].fetch("gone.eightfold.ai/gone.example")
    both_refused = FakeWeb({JOBS.format(SITE, "acme.com", 0): 403, SEARCH.format(SITE, "acme.com", 0): 403})
    with pytest.raises(SourceError, match="neither list endpoint could be read .*: HTTP 403 for /api/apply/v2/jobs; HTTP 403 for /api/pcsx/search"):
        quiet(both_refused)[0].fetch("acme.eightfold.ai/acme.com")
    closed = FakeWeb({**jobs_board(load_fixture), "https://acme.eightfold.ai/robots.txt": "User-agent: *\nDisallow: /"})
    with pytest.raises(SourceError, match="robots.txt on acme.eightfold.ai disallows"):
        quiet(closed)[0].fetch("acme.eightfold.ai/acme.com")
    assert closed.urls == ["https://acme.eightfold.ai/robots.txt"]


ROBOTS_AS_PUBLISHED = (Path(__file__).parent / "fixtures" / "robots_disallow_all_before_longer_allows.txt").read_text()


def test_the_robots_txt_these_sites_publish_allows_both_list_endpoints(load_fixture):
    # "Disallow: /" comes first in it; the longer "Allow: /api/apply" decides.
    web = FakeWeb({**jobs_board(load_fixture), f"https://{SITE}/robots.txt": ROBOTS_AS_PUBLISHED})
    assert len(quiet(web)[0].fetch(f"{SITE}/acme.com")) == 13
    own = FakeWeb({**search_board(load_fixture), f"https://{OWN_HOST}/robots.txt": ROBOTS_AS_PUBLISHED})
    assert len(quiet(own)[0].fetch(f"{OWN_HOST}/globex.example")) == 4


@pytest.mark.parametrize("first_endpoint, reason", [
    (500, "HTTP 500"), (403, "HTTP 403"), ("<html>Sign in</html>", "non-JSON response"),
    ({"error": "unknown"}, "unexpected response shape"),
])
def test_the_other_endpoint_is_tried_when_the_first_fails(load_fixture, first_endpoint, reason):
    search = load_fixture("eightfold_search_endpoint_names_fields_differently.json")
    first, second = JOBS.format(OWN_HOST, "globex.example", 0), SEARCH.format(OWN_HOST, "globex.example", 0)
    web = FakeWeb({first: first_endpoint, second: search})
    adapter, _ = quiet(web)
    assert len(adapter.fetch(f"{OWN_HOST}/globex.example")) == 4
    assert web.urls == [f"https://{OWN_HOST}/robots.txt", first, second]
    # When the other fails as well, the fetch fails and says what each one answered.
    with pytest.raises(SourceError, match=f"neither list endpoint could be read .*: {reason} for /api/apply/v2/jobs; "
                                          "HTTP 502 for /api/pcsx/search"):
        quiet(FakeWeb({first: first_endpoint, second: 502}))[0].fetch(f"{OWN_HOST}/globex.example")
    with pytest.raises(BoardNotFound):                                  # both missing: no board there
        quiet(FakeWeb({}))[0].fetch(f"{OWN_HOST}/globex.example")
    with pytest.raises(SourceError, match="neither list endpoint"):     # one missing, one broken: not "gone"
        quiet(FakeWeb({second: 500}))[0].fetch(f"{OWN_HOST}/globex.example")


def test_an_endpoint_robots_txt_refuses_is_skipped_for_the_other(load_fixture):
    search = load_fixture("eightfold_search_endpoint_names_fields_differently.json")
    first, second = JOBS.format(OWN_HOST, "globex.example", 0), SEARCH.format(OWN_HOST, "globex.example", 0)
    robots = "User-agent: *\nDisallow: /\nAllow: /api/pcsx\n"
    web = FakeWeb({first: {"count": 0, "positions": []}, second: search, f"https://{OWN_HOST}/robots.txt": robots})
    assert len(quiet(web)[0].fetch(f"{OWN_HOST}/globex.example")) == 4
    assert web.urls == [f"https://{OWN_HOST}/robots.txt", second]       # the refused endpoint is never requested
    # The second refused, the first failing: the message names both reasons, and nothing refused was read.
    other = FakeWeb({first: 500, second: search,
                     f"https://{OWN_HOST}/robots.txt": "User-agent: *\nDisallow: /api/pcsx\n"})
    with pytest.raises(SourceError, match=r"HTTP 500 for /api/apply/v2/jobs; robots.txt on careers.globex.example "
                                          r"disallows /api/pcsx/search \(line 2: Disallow: /api/pcsx\)"):
        quiet(other)[0].fetch(f"{OWN_HOST}/globex.example")
    assert second not in other.urls


def test_a_site_that_finds_us_too_busy_is_not_asked_on_the_other_endpoint(load_fixture):
    search = load_fixture("eightfold_search_endpoint_names_fields_differently.json")
    first, second = JOBS.format(OWN_HOST, "globex.example", 0), SEARCH.format(OWN_HOST, "globex.example", 0)
    web = Flaky({first: [429], second: search})
    with pytest.raises(SourceError, match="keeps answering HTTP 429"):
        quiet(web)[0].fetch(f"{OWN_HOST}/globex.example")
    assert second not in web.urls


def test_requests_are_spaced_whatever_session_is_used(load_fixture):
    now = [100.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    adapter = EightfoldAdapter(FakeWeb(jobs_board(load_fixture)), sleep=sleep, clock=lambda: now[0],
                               min_interval=eightfold.MIN_INTERVAL or 1.5)
    assert len(adapter.fetch("acme.eightfold.ai/acme.com")) == 13
    assert waits == [1.5, 1.5]                # robots.txt, then a pause before each of the two pages
    assert 1.0 < 1.5 <= adapter.min_interval  # slower than the one request per second of other sources


def test_a_429_is_retried_after_a_pause_and_slows_the_rest_of_the_fetch(load_fixture):
    board = jobs_board(load_fixture)
    first, second = JOBS.format(SITE, "acme.com", 0), JOBS.format(SITE, "acme.com", 8)
    web = Flaky({first: [429, board[first]], second: [(429, {"Retry-After": "120"}), board[second]]})
    now = [0.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    adapter = EightfoldAdapter(web, min_interval=1.5, sleep=sleep, clock=lambda: now[0])
    assert len(adapter.fetch("acme.eightfold.ai/acme.com")) == 13
    # robots.txt, 429, pause 30 s, page one; then at half the pace: 429, the pause it asked for, page two.
    assert waits == [1.5, 30.0, 3.0, 120.0]
    assert web.urls.count(first) == 2 and web.urls.count(second) == 2
    assert adapter.last_stats.complete and adapter.last_stats.requests == 5


def test_a_dropped_connection_is_retried_like_a_429(load_fixture):
    import requests
    board = jobs_board(load_fixture)
    second = JOBS.format(SITE, "acme.com", 8)
    dropped = requests.ConnectionError("Connection reset by peer")
    adapter, waits = quiet(Flaky({**board, second: [dropped, board[second]]}))
    assert len(adapter.fetch("acme.eightfold.ai/acme.com")) == 13 and waits == [30.0]
    adapter, waits = quiet(Flaky({**board, second: [dropped]}))
    with pytest.raises(SourceError, match="request for .* failed: Connection reset by peer"):
        adapter.fetch("acme.eightfold.ai/acme.com")
    assert waits == [30.0, 90.0]


def test_a_site_that_keeps_answering_429_fails_the_fetch_and_closes_nothing(conn, load_fixture):
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "eightfold", "https://acme.eightfold.ai/careers?domain=acme.com")
    assert conn.execute("SELECT ref FROM boards").fetchone()["ref"] == "acme.eightfold.ai/acme.com"
    board = jobs_board(load_fixture)
    adapter, _ = quiet(FakeWeb(board))
    run = scout.run_scout(conn, adapter_for=lambda source: adapter)
    assert [(b.status, b.roles_fetched, b.complete) for b in run.boards] == [("ok", 13, True)]

    # The next day the second page is refused, every time it is asked for.
    busy = Flaky({**board, JOBS.format(SITE, "acme.com", 8): [429]})
    adapter, waits = quiet(busy)
    with pytest.raises(SourceError, match="keeps answering HTTP 429"):
        adapter.fetch("acme.eightfold.ai/acme.com")
    assert waits == [30.0, 90.0] and busy.urls.count(JOBS.format(SITE, "acme.com", 8)) == 3
    run = scout.run_scout(conn, adapter_for=lambda source: quiet(Flaky(dict(busy.pages)))[0])
    (failed,) = run.boards
    assert failed.status == "error" and "keeps answering HTTP 429" in failed.error
    assert conn.execute("SELECT COUNT(*) FROM roles WHERE closed_at IS NULL").fetchone()[0] == 13
    assert conn.execute("SELECT COUNT(*) FROM role_descriptions").fetchone()[0] == 0     # metadata only


def test_pages_that_do_not_add_up_close_nothing_and_a_full_read_closes_what_left(conn, load_fixture):
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "eightfold", "acme.eightfold.ai/acme.com")
    board = jobs_board(load_fixture)
    first, second = JOBS.format(SITE, "acme.com", 0), JOBS.format(SITE, "acme.com", 8)
    scout.run_scout(conn, adapter_for=lambda source: quiet(FakeWeb(board))[0])
    open_roles = lambda: conn.execute("SELECT COUNT(*) FROM roles WHERE closed_at IS NULL").fetchone()[0]

    # The list moved further than the overlap covers: page two holds only roles of page one.
    moved = {first: board[first], second: {**board[second], "positions": board[first]["positions"][3:8]}}
    adapter, _ = quiet(FakeWeb(moved))
    run = scout.run_scout(conn, adapter_for=lambda source: adapter)
    assert [(b.status, b.roles_fetched, b.complete) for b in run.boards] == [("ok", 10, False)]
    assert "saw 10 of the 13 roles listed" in run.boards[0].note and open_roles() == 13

    # Complete reads with three roles gone close exactly those, by the usual rule: on the second miss.
    shorter = {first: {**board[first], "count": 10}}
    run = scout.run_scout(conn, adapter_for=lambda source: quiet(FakeWeb(shorter))[0])
    assert [(b.status, b.roles_fetched, b.complete) for b in run.boards] == [("ok", 10, True)]
    assert open_roles() == 13
    scout.run_scout(conn, adapter_for=lambda source: quiet(FakeWeb(shorter))[0])
    assert open_roles() == 10


# --- eightfold: resolve, gaps and the CLI --------------------------------------

def test_resolve_maps_a_company_from_a_link_to_its_eightfold_site(conn, load_fixture):
    res, boards, web = resolve_one(conn, "Acme", "https://acme.example", {
        "https://acme.example": '<a href="/careers">Careers</a>',
        "https://acme.example/careers": '<a href="https://acme.eightfold.ai/careers?domain=acme.com">Open roles</a>',
        **jobs_board(load_fixture)})
    assert res.outcome == "mapped" and boards == [("eightfold", "acme.eightfold.ai/acme.com", "link")]
    (cand,) = res.candidates
    assert (cand.roles, cand.detail) == (13, "linked from https://acme.example/careers")
    assert JOBS.format(SITE, "acme.com", 8) not in web.urls           # one page confirms a board


def test_a_company_recorded_on_eightfold_before_maps_from_the_recorded_url(conn, load_fixture):
    companies.add_company(conn, "Acme", website="https://acme.example")
    record_unsupported(conn, "Acme", "eightfold", "https://acme.eightfold.ai/careers")
    # The system has an adapter now: the gaps report asks for a mapping, not for a connector.
    entry = next(e for e in gaps.classify(conn) if e["company"] == "Acme")
    assert (entry["state"], entry["system"]) == ("needs_mapping", "eightfold")
    assert [t["name"] for t in recheck_targets(conn, [])] == ["Acme"]

    web = FakeWeb({**jobs_board(load_fixture),
                   "https://acme.eightfold.ai/careers": '<a href="/careers?domain=acme.com">All jobs</a>'})
    (res,) = resolve_companies(conn, resolver=Resolver(session=web))
    assert res.outcome == "mapped" and not any("acme.example" in u for u in web.urls)
    assert tuple(conn.execute("SELECT source, ref, origin FROM boards").fetchone()) == (
        "eightfold", "acme.eightfold.ai/acme.com", "link")
    assert next(e for e in gaps.classify(conn) if e["company"] == "Acme")["state"] == "covered"


def test_a_page_that_only_shows_signs_of_eightfold_asks_for_a_mapping_by_hand(conn):
    res, boards, web = resolve_one(conn, "Acme", "https://acme.example", {
        "https://acme.example": '<a href="/careers">Careers</a>',
        "https://acme.example/careers": '<script src="https://static.vscdn.net/eightfold-careers/app.js"></script>'})
    assert boards == [] and (res.outcome, res.job_system) == ("needs_review", "eightfold")
    assert 'map it by hand: rolescout companies map "Acme" eightfold BOARD' in res.notes[-1]
    assert not any("eightfold.ai" in u for u in web.urls)              # no address is guessed from a name
    entry = next(e for e in gaps.classify(conn) if e["company"] == "Acme")
    assert (entry["state"], entry["system"]) == ("needs_mapping", "eightfold")


def test_map_accepts_an_eightfold_url_and_the_google_reference(conn, capsys):
    companies.add_company(conn, "Globex")
    assert cli.main(["companies", "map", "Globex", "eightfold",
                     "https://careers.globex.example/careers/job/9?domain=globex.example"]) == 0
    assert "Globex -> eightfold/careers.globex.example/globex.example" in capsys.readouterr().out
    assert cli.main(["companies", "map", "Globex", "eightfold", "https://careers.globex.example/"]) == 1
    assert "is not an Eightfold career site" in capsys.readouterr().err
    companies.add_company(conn, "Google")
    assert cli.main(["companies", "map", "Google", "google",
                     "https://www.google.com/about/careers/applications/jobs/results"]) == 0
    assert "Google -> google/careers" in capsys.readouterr().out
    assert cli.main(["companies", "map", "Globex", "google", "https://careers.globex.example/"]) == 1
    assert "is not the Google career site" in capsys.readouterr().err


# --- the Google career site ----------------------------------------------------

def feed():
    return FEED_FIXTURE.read_text(encoding="utf-8")


def test_google_feed_gives_every_role_with_its_employer():
    web = FakeWeb({FEED_URL: feed()})
    adapter = GoogleAdapter(web)
    roles = adapter.fetch("careers", company="Google")
    assert [r.title for r in roles] == [
        "Product Manager", "Research Scientist, Robotics", "Program Manager, Partner Operations",
        "Account Executive, Public Sector", "Senior Security Engineer"]
    pm, other, hybrid, remote, zurich = roles
    assert (pm.source, pm.source_job_id, pm.company) == ("google", "100000000000000001", "Google")
    assert pm.location == "Mountain View, CA, USA; New York, NY, USA; Irvine, CA, USA"
    assert (pm.department, pm.workplace_type, pm.date_posted) == ("Google: Product Management", "onsite", "2026-11-13")
    assert pm.url == "https://careers.google.com/jobs/results/100000000000000001-product-manager/"
    # A role another employer in the feed is hiring for: same company, employer kept on the role.
    assert (other.company, other.department, other.location) == (
        "Google", "Acme Labs: Software Engineering", "London, UK")
    assert (hybrid.workplace_type, remote.workplace_type) == ("hybrid", "remote")
    assert remote.department == "Google" and remote.location.startswith("Arkansas, USA; Arizona, USA")
    assert zurich.location == "Zürich, Switzerland"
    # With no context (rolescout fetch) every description comes back, as plain text.
    assert all(r.description == "Minimum qualifications: Shortened for the fixture."
               and r.compensation_text == "" for r in roles)
    assert web.urls == ["https://www.google.com/robots.txt", FEED_URL]
    assert (adapter.last_stats.requests, adapter.last_stats.complete) == (2, True)
    assert adapter.confirm("https://careers.google.com/jobs/results/") == ("careers", 5)
    assert not GoogleAdapter.guessable


def test_google_descriptions_are_stored_for_every_open_role_and_deleted_when_one_closes(conn):
    """The feed carries them at no extra request, as the one-request boards do."""
    from rolescout.adapters import FetchContext
    assert not GoogleAdapter.uses_context
    adapter = GoogleAdapter(FakeWeb({FEED_URL: feed()}))
    assert all(r.description for r in adapter.fetch("careers"))
    assert all(r.description for r in adapter.fetch("careers", context=FetchContext()))   # whatever the caller wants

    # Through a scout whose ranking wants one title only: all five are stored.
    companies.add_company(conn, "Google")
    companies.add_board(conn, "Google", "google", "careers")
    scout.run_scout(conn, adapter_for=lambda source: GoogleAdapter(FakeWeb({FEED_URL: feed()})),
                    wants_description=lambda title: title.startswith("Senior Security"))
    stored = dict(conn.execute("SELECT r.title, d.description FROM role_descriptions d JOIN roles r ON r.id = d.role_id"))
    assert len(stored) == 5
    assert stored["Senior Security Engineer"] == "Minimum qualifications: Shortened for the fixture."

    # A role that closes loses its description, unless it is saved or applied.
    gone, saved = "100000000000000005", "100000000000000003"
    conn.execute("INSERT INTO role_marks (role_id, status, marked_at) SELECT id, 'saved', 'x' FROM roles "
                 "WHERE source_job_id = ?", (saved,))
    shorter = feed().replace(f"<jobid>{gone}</jobid>", "<jobid></jobid>").replace(f"<jobid>{saved}</jobid>", "<jobid></jobid>")
    for _ in range(2):                                         # closed on the second miss
        scout.run_scout(conn, adapter_for=lambda source: GoogleAdapter(FakeWeb({FEED_URL: shorter})))
    left = {r["source_job_id"]: (r["closed_at"] is not None, r["description"] is not None) for r in conn.execute(
        "SELECT r.source_job_id, r.closed_at, d.description FROM roles r "
        "LEFT JOIN role_descriptions d ON d.role_id = r.id")}
    assert left[gone] == (True, False) and left[saved] == (True, True)
    assert sum(1 for closed, described in left.values() if not closed and described) == 3


def test_google_reference_is_one_board_whatever_url_names_it():
    for text in ("careers", "Careers", "google", "https://www.google.com/about/careers/applications/jobs/results?q=x",
                 "careers.google.com/jobs/results/1-x/", FEED_URL):
        assert GoogleAdapter.canonical_ref(text) == "careers"
    for text in ("https://www.google.com/search?q=jobs", "https://careers.globex.example/", "acme", ""):
        with pytest.raises(ValueError, match="is not the Google career site"):
            GoogleAdapter.canonical_ref(text)
    assert google.parse_ref("careers") == "careers"


def test_scout_stores_google_roles_and_a_broken_feed_closes_nothing(conn):
    companies.add_company(conn, "Google", website="google.com")
    companies.add_board(conn, "Google", "google", "careers")
    run = scout.run_scout(conn, adapter_for=lambda source: GoogleAdapter(FakeWeb({FEED_URL: feed()})))
    assert [(b.status, b.roles_fetched, b.requests, b.complete) for b in run.boards] == [("ok", 5, 2, True)]
    rows = conn.execute("SELECT c.name, r.department FROM roles r JOIN companies c ON c.id = r.company_id "
                        "ORDER BY r.source_job_id").fetchall()
    assert {r["name"] for r in rows} == {"Google"} and rows[1]["department"] == "Acme Labs: Software Engineering"
    assert conn.execute("SELECT COUNT(*) FROM role_descriptions").fetchone()[0] == 5
    open_roles = lambda: conn.execute("SELECT COUNT(*) FROM roles WHERE closed_at IS NULL").fetchone()[0]

    empty = '<?xml version="1.0" encoding="UTF-8"?><jobs><updated>2026-11-18T00:00:00+00:00</updated></jobs>'
    for broken, message in ((empty, "lists no roles"), ("<jobs><job>", "not well-formed XML"),
                            ("<html><body>Sorry</body></html>", "lists no roles"), (503, "HTTP 503")):
        run = scout.run_scout(conn, adapter_for=lambda source: GoogleAdapter(FakeWeb({FEED_URL: broken})))
        (board,) = run.boards
        assert board.status == "error" and message in board.error, (broken, board.error)
        assert open_roles() == 5

    # A role gone from a good feed closes as on any board: on the second miss.
    shorter = feed().replace("<jobid>100000000000000005</jobid>", "<jobid></jobid>")
    scout.run_scout(conn, adapter_for=lambda source: GoogleAdapter(FakeWeb({FEED_URL: shorter})))
    assert open_roles() == 5
    scout.run_scout(conn, adapter_for=lambda source: GoogleAdapter(FakeWeb({FEED_URL: shorter})))
    assert open_roles() == 4


def test_google_feed_is_not_read_when_robots_disallows_it():
    web = FakeWeb({FEED_URL: feed(), "https://www.google.com/robots.txt":
                   "User-agent: *\nDisallow: /about/careers/applications/jobs/feed.xml"})
    with pytest.raises(SourceError, match="robots.txt on www.google.com disallows"):
        GoogleAdapter(web).fetch("careers")
    assert web.urls == ["https://www.google.com/robots.txt"]
    with pytest.raises(BoardNotFound):
        GoogleAdapter(FakeWeb({})).fetch("careers")
