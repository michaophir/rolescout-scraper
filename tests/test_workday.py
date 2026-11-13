"""The Workday adapter, offline, against responses recorded from live boards.

``workday_paging.json`` and ``workday_underscore_tenant.json`` hold every
exchange of a few real fetches, anonymized (see tests/refresh_fixtures.py); a request that was not
recorded fails the test, so the request counts here are exact. Expected
values belong to the roles recorded; update them after re-recording.
"""

import json
from datetime import date

import pytest

from rolescout import cli, companies, db, scout
from rolescout.adapters import (
    BoardNotFound, FetchContext, KnownRole, SourceError, WorkdayAdapter,
)
from rolescout.adapters.workday import (
    WorkdayRef, find_refs, is_workday_url, parse_ref, parse_workday_url, posted_date, split_facet,
)
from rolescout.resolve import Resolver, resolve_companies
from rolescout.shortlist import parse_config
from tests.conftest import FIXTURES, FakeWeb

HOST = "https://acme.wd5.myworkdayjobs.com"
API = HOST + "/wday/cxs/acme/Careers"
BOARD = "acme/wd5/Careers"
PM_ID = "acme/careers/Product-Manager-III--Cybersecurity-Integrations_JR2130"
SAVED_ID = "acme/careers/Account-Executive---SLED---NY-NJ_JR2140-1"
OLD_ID = "acme/careers/Product-Manager-Cyber-Intel_JR2106-1"      # "Posted 30+ Days Ago"


class Response:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


class Replay:
    """Serves a recording. An unrecorded request is a test failure."""

    def __init__(self, name):
        cassette = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        self.today = date.fromisoformat(cassette["recorded_on"])
        self.exchanges = cassette["exchanges"]
        self.calls = []       # (method, url, offset or None)
        self.parts = []       # the facet filters of the searches that had one

    def request(self, method, url, **kwargs):
        body = kwargs.get("json")
        for e in self.exchanges:
            if (e["method"], e["url"], e["json"]) == (method, url, body):
                self.calls.append((method, url, body["offset"] if body else None))
                if body and body["appliedFacets"]:
                    self.parts.append(body["appliedFacets"])
                return Response(e["status"], e["body"])
        raise AssertionError(f"request was not recorded: {method} {url} {body}")

    def adapter(self, **kwargs):
        return WorkdayAdapter(self, min_interval=0, today=lambda: self.today, **kwargs)


class Scripted:
    """Answers each request with a function of (method, url, json body)."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        result = self.answer(method, url, kwargs.get("json"))
        return result if isinstance(result, Response) else Response(200, result)


def posting(n):
    return {"title": f"Role {n}", "externalPath": f"/job/Here/Role-{n}_R{n}",
            "locationsText": "Here", "postedOn": "Posted Today"}


def board_of(total, robots="", statuses=None):
    """A fake board of ``total`` postings that, like the real endpoint, reports
    the total on the first page only and answers an offset past the end with
    the first page."""
    statuses = dict(statuses or {})

    def answer(method, url, body):
        if url.endswith("/robots.txt"):
            return Response(200, robots) if robots else Response(404, "")
        if url in statuses:
            return Response(statuses[url], {})
        if method == "POST":
            offset = body["offset"] if body["offset"] < total else 0
            page = [posting(n) for n in range(offset, min(offset + 20, total))]
            return {"total": total if body["offset"] == 0 or offset == 0 else 0, "jobPostings": page}
        return {"jobPostingInfo": {"jobDescription": "<p>Text</p>", "startDate": "2026-01-02"}}
    return Scripted(answer)


def quiet(session, **kwargs):
    return WorkdayAdapter(session, min_interval=0, sleep=lambda s: None,
                          today=lambda: date(2026, 11, 13), **kwargs)


# --- references ---------------------------------------------------------------

# Every shape of URL a career site hands out, with the board it names.
URL_SHAPES = [
    ("https://acme.wd5.myworkdayjobs.com/Careers", "acme/wd5/Careers"),             # the front page
    ("https://acme.wd5.myworkdayjobs.com/en-US/Careers/", "acme/wd5/Careers"),      # locale, trailing slash
    ("https://acme.wd5.myworkdayjobs.com/en-US/Careers", "acme/wd5/Careers"),
    ("https://acme.wd503.myworkdayjobs.com/ExternalSite/job/IND-CHENNAI/Senior-Product-Manager-II_R10000776",
     "acme/wd503/ExternalSite"),                                                     # a job page
    ("https://acme.wd1.myworkdayjobs.com/EXT/job/GA-ATLANTA-740-W-PEACHTREE-ST-NW/AI-Solutions-Engineer-Lead-Senior_JR211739/apply",
     "acme/wd1/EXT"),                                                                # an apply page
    ("https://acme.wd3.myworkdayjobs.com/Acme_Career_Site/login", "acme/wd3/Acme_Career_Site"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/2/userHome", "acme/wd5/2"),          # numeric site
    ("https://acme.wd5.myworkdayjobs.com/External_Jobs/jobs", "acme/wd5/External_Jobs"),
    ("https://acme.wd115.myworkdayjobs.com/Careers?locationCountry=54c5b6971ffb4bf0b116fe7651ec789a",
     "acme/wd115/Careers"),                                                          # query string
    ("https://acme.wd5.myworkdayjobs.com/Careers/?source=Careers_Website", "acme/wd5/Careers"),
    ("https://parentco.wd3.myworkdayjobs.com/en-US/AcmeLegal/introduceYourself?clientRequestID=1c27181131e64261b844ecb708046ec2",
     "parentco/wd3/AcmeLegal"),                                                      # tenant is not the company
    ("https://acme-corp.wd5.myworkdayjobs.com/AcmeCareers", "acme-corp/wd5/AcmeCareers"),   # hyphen in the tenant
    ("https://acme.wd5.myworkdayjobs.com/Acme-Life-Careers", "acme/wd5/Acme-Life-Careers"),  # hyphen in the site
    ("https://acme.wd1.myworkdayjobs.com/acme", "acme/wd1/acme"),                   # lowercase site
    ("https://wd1.myworkdaysite.com/en-US/recruiting/acme/AcmeCareers/login",
     "acme/wd1/AcmeCareers"),                                                        # the myworkdaysite.com form
]


@pytest.mark.parametrize("url, ref", URL_SHAPES)
def test_reference_from_every_url_shape(url, ref):
    assert str(parse_workday_url(url)) == ref
    assert WorkdayAdapter.canonical_ref(url) == ref     # what `companies map` stores
    assert WorkdayAdapter.canonical_ref(ref) == ref
    assert is_workday_url(url)


def test_a_url_on_another_host_is_never_a_workday_board():
    # A company's own site, even one whose pages mention Workday.
    assert parse_workday_url("https://www.example.com/") is None
    assert not is_workday_url("https://www.example.com/")


def test_reference_parts_and_urls():
    ref = parse_workday_url("https://parentco.wd3.myworkdayjobs.com/en-US/AcmeLegal/login")
    assert ref == WorkdayRef(tenant="parentco", data_center="wd3", site="AcmeLegal")
    assert ref.api == "https://parentco.wd3.myworkdayjobs.com/wday/cxs/parentco/AcmeLegal"
    assert ref.job_url("/job/Dayton-OH/X_R1") == \
        "https://parentco.wd3.myworkdayjobs.com/AcmeLegal/job/Dayton-OH/X_R1"
    # The API URL itself, a URL without a scheme, capitals in the host.
    assert str(parse_workday_url(
        "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs")) == "acme/wd5/Careers"
    assert str(parse_workday_url("Acme.WD5.myworkdayjobs.com/Careers")) == "acme/wd5/Careers"
    assert str(parse_workday_url(
        "https://wd1.myworkdaysite.com/wday/cxs/acme/AcmeCareers/jobs")) == "acme/wd1/AcmeCareers"
    # An underscore in the tenant is a hyphen in its hostname.
    alias = parse_ref("acme_corp/wd5/Careers")
    assert alias.host == "acme-corp.wd5.myworkdayjobs.com"
    assert alias.api.endswith("/wday/cxs/acme_corp/Careers")


@pytest.mark.parametrize("url", [
    "https://www.example.com/",
    "https://www.example.com/?next=myworkdayjobs.com",
    "https://myworkdayjobs.com/Careers",                         # no tenant, no data center
    "https://acme.myworkdayjobs.com/Careers",                    # no data center
    "https://acme.wd5.myworkdayjobs.com/",                       # Workday, but names no site
    "https://acme.wd5.myworkdayjobs.com/en-US",
    "https://acme.wd5.myworkdayjobs.com.evil.com/Careers",       # look-alike hosts
    "https://evil.com/acme.wd5.myworkdayjobs.com/Careers",
    "https://acme.wd5.notmyworkdayjobs.com/Careers",
    "https://wd1.myworkdaysite.com/en-US/AcmeCareers",       # not under /recruiting/
    "https://wd1.myworkdaysite.com/recruiting/acme",
    "https://acme.wd5.myworkdayjobs.com/bad site",
    "", "not a url",
])
def test_urls_that_name_no_workday_board(url):
    assert parse_workday_url(url) is None
    with pytest.raises(ValueError, match="not a Workday board"):
        parse_ref(url)


def test_refs_found_in_page_text():
    text = '''
        <a href="https://parentco.wd3.myworkdayjobs.com/en-US/AcmeLegal/job/Dayton-OH/X_R1">a</a>
        <a href="https://parentco.wd3.myworkdayjobs.com/acmelegal/login">same site again</a>
        <a href="https://wd1.myworkdaysite.com/recruiting/acme/AcmeCareers">b</a>
        we integrate with myworkdayjobs.com and https://acme.wd5.myworkdayjobs.com.evil.com/Jobs
        see https://acme.wd5.myworkdayjobs.com for more.
    '''
    refs, bare = find_refs(text)
    assert [str(r) for r in refs] == ["parentco/wd3/AcmeLegal", "acme/wd1/AcmeCareers"]
    assert bare == "https://acme.wd5.myworkdayjobs.com"
    assert find_refs("careers on myworkdayjobs.com, www.example.com") == ([], "")


def test_posted_date_is_derived_only_when_the_text_allows_it():
    today = date(2026, 11, 13)
    assert posted_date("Posted Today", today) == "2026-11-13"
    assert posted_date("Posted Yesterday", today) == "2026-11-12"
    assert posted_date("Posted 2 Days Ago", today) == "2026-11-11"
    assert posted_date("Posted 30 Days Ago", today) == "2026-10-14"
    assert posted_date("Posted 30+ Days Ago", today) == ""      # only a lower bound
    assert posted_date(None, today) == posted_date("", today) == ""   # some boards list no posted text
    assert posted_date("Publié aujourd'hui", today) == ""


# --- paging -------------------------------------------------------------------

def test_paging_reads_every_role_and_stops_at_the_total():
    web = Replay("workday_paging.json")
    adapter = web.adapter()
    roles = adapter.fetch(BOARD, company="Acme")

    # robots.txt, then one request per 20 roles. Page two reports total 0;
    # the count from page one is what stops the paging.
    assert web.calls == [("GET", HOST + "/robots.txt", None),
                         ("POST", API + "/jobs", 0), ("POST", API + "/jobs", 20)]
    assert len(roles) == 23 and len({r.source_job_id for r in roles}) == 23
    stats = adapter.last_stats
    assert (stats.requests, stats.complete, stats.descriptions_fetched, stats.notes) == (3, True, 0, [])

    first = roles[0]
    assert first.source == "workday" and first.company == "Acme"
    assert first.source_job_id == \
        "acme/careers/Customer-Success-Manager--International-Public-Sector_JR2144"
    assert first.title == "Customer Success Manager, International Public Sector"
    assert first.url == ("https://acme.wd5.myworkdayjobs.com/Careers/job/London-UK/"
                         "Customer-Success-Manager--International-Public-Sector_JR2144")
    assert first.date_posted == "2026-11-12"                       # "Posted Yesterday"
    assert first.location == "London UK (+2 more)"                 # the list says "3 Locations"
    assert first.description == ""                                 # nobody asked for it
    by_id = {r.source_job_id: r for r in roles}
    assert by_id[SAVED_ID].title == "Account Executive, (SLED) NY/NJ"      # double space collapsed
    assert (by_id[PM_ID].location, by_id[PM_ID].workplace_type) == ("US Remote (+1 more)", "remote")
    assert by_id[OLD_ID].date_posted == ""                         # "Posted 30+ Days Ago"
    assert roles[4].location == "Arlington, VA" and roles[4].date_posted == "2026-11-06"
    assert roles[16].date_posted == "2026-10-14"                   # "Posted 30 Days Ago" is exact
    assert all(r.department == "" and r.compensation_text == "" for r in roles)


def test_an_offset_past_the_end_is_never_requested():
    # 40 roles fill two pages exactly. A third request would get page one back.
    web = board_of(40)
    roles = quiet(web).fetch("acme/wd5/Jobs")
    assert len(roles) == 40
    assert [body["offset"] for method, _, body in web.calls if method == "POST"] == [0, 20]


def test_an_empty_board_is_not_an_error():
    assert quiet(board_of(0)).fetch("acme/wd5/Jobs") == []


def test_a_board_over_the_cap_is_read_in_parts():
    """The recorded board read as if a search stopped at 20 results: same 23 roles."""
    web = Replay("workday_paging.json")
    adapter = web.adapter(result_cap=20)
    roles = adapter.fetch(BOARD)

    whole = Replay("workday_paging.json").adapter().fetch(BOARD)
    assert {r.source_job_id for r in roles} == {r.source_job_id for r in whole}
    assert len(roles) == 23
    stats = adapter.last_stats
    assert stats.complete
    assert stats.notes == ["more than 20 roles: read in 15 parts, by locations"]
    assert stats.requests == 2 + 15       # robots, the capped first page, one page per part
    assert len(web.parts) == 15 and all(list(part) == ["locations"] and len(part["locations"]) == 1
                                        for part in web.parts)
    assert len({part["locations"][0] for part in web.parts}) == 15


def test_the_real_capped_board_splits_by_country(load_fixture):
    first = load_fixture("workday_capped_first_page.json")
    assert first["total"] == 2000                                  # the cap, not the true count
    param, value_ids = split_facet(first["facets"], {}, 2000)
    assert param == "Country_and_Jurisdiction" and len(value_ids) == 55
    counts = {f["facetParameter"]: [v["count"] for v in f["values"]]
              for f in first["facets"] if "count" in f["values"][0]}
    assert max(counts["Country_and_Jurisdiction"]) < 2000 < sum(counts["Country_and_Jurisdiction"])
    # A facet already applied is not chosen again; with nothing usable, no split.
    assert split_facet(first["facets"], {"Country_and_Jurisdiction": ["x"]}, 2000)[0] != \
        "Country_and_Jurisdiction"
    assert split_facet([], {}, 2000) is None
    assert split_facet([{"facetParameter": "timeType", "values": [{"id": "a", "count": 9}]}], {}, 5) is None


def test_a_capped_search_nothing_splits_is_marked_incomplete():
    def answer(method, url, body):
        if url.endswith("/robots.txt"):
            return Response(404, "")
        offset = body["offset"]
        return {"total": 40 if offset == 0 else 0, "facets": [],
                "jobPostings": [posting(n) for n in range(offset, offset + 20)]}
    web = Scripted(answer)
    adapter = quiet(web, result_cap=40)
    roles = adapter.fetch("acme/wd5/Jobs")
    assert len(roles) == 40 and adapter.last_stats.complete is False
    assert "capped at 40 roles" in adapter.last_stats.notes[0]


# --- the description rule -----------------------------------------------------

def wants_manager(title):
    return "manager" in title.lower()


def test_descriptions_are_fetched_only_for_wanted_titles():
    web = Replay("workday_paging.json")
    adapter = web.adapter(max_descriptions=3)
    roles = adapter.fetch(BOARD, context=FetchContext(wants_description=wants_manager))

    details = [url for method, url, _ in web.calls if method == "GET" and "/job/" in url]
    assert details == [
        API + "/job/London-UK/Customer-Success-Manager--International-Public-Sector_JR2144",
        API + "/job/US-Remote/Product-Manager-III--Cybersecurity-Integrations_JR2130",
        API + "/job/New-York-NY/Customer-Success-Manager_JR2141"]
    described = [r for r in roles if r.description]
    assert [r.title for r in described] == [
        "Customer Success Manager, International Public Sector",
        "Product Manager III, Cybersecurity Integrations", "Customer Success Manager"]
    assert all(r.description == "" for r in roles if not wants_manager(r.title))
    stats = adapter.last_stats
    # Eight titles say "manager"; three per fetch were allowed, five wait.
    assert (stats.requests, stats.descriptions_fetched, stats.descriptions_waiting) == (6, 3, 5)
    assert stats.complete and stats.notes == ["5 description(s) left for the next run"]

    first = described[0]
    assert first.description.startswith("See yourself at Acme Acme is seeking a self-motivated")
    assert len(first.description) == 9073 and "<" not in first.description[:300]
    # The detail gives every location and the exact date.
    assert first.location == "London, UK; Denmark - Remote; DEN - Remote"
    assert first.workplace_type == "remote"
    assert first.date_posted == "2026-11-12"


def test_no_description_is_fetched_twice_and_stored_values_are_kept():
    web = Replay("workday_paging.json")
    adapter = web.adapter()
    known = {r.source_job_id: KnownRole(has_description=True, date_posted="2026-09-01",
                                        location="Stored, Place", workplace_type="hybrid")
             for r in Replay("workday_paging.json").adapter().fetch(BOARD)}
    roles = adapter.fetch(BOARD, context=FetchContext(wants_description=lambda t: True, known=known))

    assert adapter.last_stats.requests == 3                 # robots and two pages: no detail
    by_id = {r.source_job_id: r for r in roles}
    # "30+ days ago" no longer blanks a date that is on record; the fuller
    # location from the earlier detail request stays too.
    assert by_id[OLD_ID].date_posted == "2026-09-01"
    assert by_id[OLD_ID].location == "Stored, Place"
    assert by_id[SAVED_ID].workplace_type == "hybrid"
    assert by_id[PM_ID].workplace_type == "remote"          # what the list says wins


def test_saved_and_applied_roles_get_a_description_whatever_their_title():
    web = Replay("workday_paging.json")
    adapter = web.adapter()
    roles = adapter.fetch(BOARD, context=FetchContext(known={SAVED_ID: KnownRole(keep=True)}))

    assert [r.source_job_id for r in roles if r.description] == [SAVED_ID]
    assert (adapter.last_stats.requests, adapter.last_stats.descriptions_fetched) == (4, 1)
    saved = next(r for r in roles if r.source_job_id == SAVED_ID)
    assert saved.location == "New York, NY; US Remote; NY - Hybrid"


def test_a_role_that_closed_before_its_description_was_read_is_kept_without_one():
    gone = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Jobs/job/Here/Role-1_R1"
    web = board_of(3, statuses={gone: 404})
    adapter = quiet(web)
    roles = adapter.fetch("acme/wd5/Jobs", context=FetchContext(wants_description=lambda t: True))
    assert [bool(r.description) for r in roles] == [True, False, True]
    assert roles[0].description == "Text" and roles[0].date_posted == "2026-01-02"
    assert adapter.last_stats.descriptions_fetched == 2


# --- tenant names, errors, limits ----------------------------------------------

def test_a_tenant_spelled_with_a_hyphen_in_its_hostname():
    """The career site's URL says acme-corp; the tenant is acme_corp."""
    url = "https://acme-corp.wd5.myworkdayjobs.com/Careers"
    web = Replay("workday_underscore_tenant.json")
    adapter = web.adapter()
    roles = adapter.fetch(url)
    base = "https://acme-corp.wd5.myworkdayjobs.com/wday/cxs/"
    assert web.calls[1:] == [("POST", base + "acme-corp/Careers/jobs", 0),   # 422
                             ("POST", base + "acme_corp/Careers/jobs", 0),
                             ("POST", base + "acme_corp/Careers/jobs", 20)]
    assert len(roles) == 22
    assert roles[0].source_job_id == "acme-corp/careers/Engineer-I_R-01440"
    assert roles[0].url == ("https://acme-corp.wd5.myworkdayjobs.com/Careers"
                            "/job/New-York---New-York/Engineer-I_R-01440")
    # The same adapter remembers which spelling answered.
    web.calls.clear()
    adapter.fetch(url)
    assert len(web.calls) == 3
    # `companies resolve` stores the spelling that works, and ids do not depend on it.
    assert Replay("workday_underscore_tenant.json").adapter().confirm(url) == ("acme_corp/wd5/Careers", 22)
    again = Replay("workday_underscore_tenant.json")
    assert again.adapter().fetch("acme_corp/wd5/Careers")[0].source_job_id == \
        roles[0].source_job_id
    assert len(again.calls) == 3


def test_missing_boards_and_bad_references():
    jobs = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Jobs/jobs"
    with pytest.raises(BoardNotFound, match="no career site 'Jobs'"):
        quiet(board_of(1, statuses={jobs: 404})).fetch("acme/wd5/Jobs")
    with pytest.raises(BoardNotFound, match="no tenant 'acme' on data center wd5"):
        quiet(board_of(1, statuses={jobs: 422})).fetch("acme/wd5/Jobs")
    with pytest.raises(SourceError, match="not a Workday board"):
        quiet(board_of(1)).fetch("acme")
    with pytest.raises(SourceError, match="unexpected response shape"):
        quiet(Scripted(lambda m, u, b: Response(404, "") if "robots" in u else {"oops": 1})).fetch(
            "acme/wd5/Jobs")


def test_a_busy_server_is_retried_once_then_reported():
    jobs = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Jobs/jobs"
    waits = []
    answers = iter([Response(404, ""), Response(503, {}, {"Retry-After": "7"}),
                    Response(200, {"total": 1, "jobPostings": [posting(0)]})])
    adapter = WorkdayAdapter(Scripted(lambda m, u, b: next(answers)), min_interval=0, sleep=waits.append)
    assert len(adapter.fetch("acme/wd5/Jobs")) == 1
    assert waits == [7.0] and adapter.last_stats.requests == 3

    with pytest.raises(SourceError, match="HTTP 500"):
        quiet(board_of(1, statuses={jobs: 500})).fetch("acme/wd5/Jobs")


def test_robots_txt_is_read_against_the_endpoint_actually_fetched():
    # One recorded board's file disallows its site's pages ("/2/"), not the JSON endpoint.
    pages_only = "User-agent: *\nDisallow: /2/\nDisallow: /refreshFacet/\n"
    assert len(quiet(board_of(2, robots=pages_only)).fetch("acme/wd5/2")) == 2
    blocked = board_of(2, robots="User-agent: *\nDisallow: /wday/\n")
    with pytest.raises(SourceError, match=r"robots.txt on acme.wd5.myworkdayjobs.com disallows "
                                          r"/wday/cxs/acme/Jobs/jobs"):
        quiet(blocked).fetch("acme/wd5/Jobs")
    assert len(blocked.calls) == 1                           # nothing after robots.txt
    with pytest.raises(SourceError, match="robots.txt"):
        quiet(Scripted(lambda m, u, b: Response(503, ""))).fetch("acme/wd5/Jobs")


def test_requests_are_at_least_a_second_apart():
    now = [100.0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    web = board_of(45)
    adapter = WorkdayAdapter(web, sleep=sleep, clock=lambda: now[0], today=lambda: date(2026, 1, 1))
    adapter.fetch("acme/wd5/Jobs")
    assert len(web.calls) == 4 and waits == [1.0, 1.0, 1.0]   # robots, then three pages


def test_a_board_that_uses_its_request_budget_stops_and_says_so():
    web = board_of(200)
    adapter = quiet(web, max_requests=4)                     # robots and three pages
    roles = adapter.fetch("acme/wd5/Jobs", context=FetchContext(wants_description=lambda t: True))
    assert len(roles) == 60 and len(web.calls) == 4
    stats = adapter.last_stats
    assert stats.complete is False and "stopped early: it used its 4 requests" in stats.notes[0]
    assert stats.descriptions_waiting == 60                  # none left to spend on descriptions

    # Out of budget before a single role: an error, so nothing is recorded as seen.
    with pytest.raises(SourceError, match="before any role was read"):
        quiet(board_of(200), max_requests=1).fetch("acme/wd5/Jobs")


def test_a_board_that_uses_its_time_budget_stops_and_says_so():
    now = [0.0]

    def tick():
        now[0] += 10.0
        return now[0]

    adapter = WorkdayAdapter(board_of(200), min_interval=0, sleep=lambda s: None, clock=tick,
                             board_seconds=95.0, today=lambda: date(2026, 1, 1))
    roles = adapter.fetch("acme/wd5/Jobs")
    assert 0 < len(roles) < 200 and adapter.last_stats.complete is False
    assert "ran for its 95 seconds" in adapter.last_stats.notes[0]


# --- in the scout ---------------------------------------------------------------

RANKING = parse_config('''
[title]
exclude = ["Cyber"]
[title.wanted]
"Product Manager" = 30
''')


def stored(conn):
    return {r["source_job_id"]: dict(r) for r in conn.execute(
        "SELECT r.*, d.description FROM roles r LEFT JOIN role_descriptions d ON d.role_id = r.id")}


def test_scout_stores_every_role_and_only_the_wanted_descriptions(conn):
    companies.add_company(conn, "Acme", tier=2)
    # Mapped by pasting a URL from the career site.
    companies.add_board(conn, "Acme", "workday", HOST + "/en-US/Careers/login")
    assert conn.execute("SELECT ref FROM boards").fetchone()["ref"] == BOARD
    # "Product Manager" is wanted, "Cyber" is excluded: of the two product
    # manager roles, only "Product Manager III, Cybersecurity Integrations" qualifies.
    assert RANKING.wants_title("Product Manager III, Cybersecurity Integrations")
    assert not RANKING.wants_title("Product Manager Cyber Intel")

    web = Replay("workday_paging.json")
    adapter = web.adapter()
    run = scout.run_scout(conn, adapter_for=lambda source: adapter,
                          wants_description=RANKING.wants_title)
    (board,) = run.boards
    assert (board.status, board.roles_fetched, board.requests, board.complete) == ("ok", 23, 4, True)
    rows = stored(conn)
    assert len(rows) == 23
    assert [k for k, r in rows.items() if r["description"]] == [PM_ID]
    assert rows[PM_ID]["description"].startswith("See yourself at Acme At Acme, you will drive")
    assert rows[PM_ID]["date_posted"] == "2026-11-12"
    assert rows[PM_ID]["location"] == "US Remote; New York, NY"
    assert rows[SAVED_ID]["description"] is None            # stored without one
    assert rows[SAVED_ID]["url"].startswith(HOST + "/Careers/job/New-York-NY/")
    logged = conn.execute("SELECT requests, note FROM run_boards").fetchone()
    assert tuple(logged) == (4, "")

    # Saving a role whose title is not wanted gets its description at the next
    # scout. The one already stored is not fetched again and is not blanked.
    conn.execute("INSERT INTO role_marks (role_id, status, marked_at) VALUES (?, 'saved', 'x')",
                 (rows[SAVED_ID]["id"],))
    web.calls.clear()
    run = scout.run_scout(conn, adapter_for=lambda source: adapter,
                          wants_description=RANKING.wants_title)
    assert [url.rsplit("/", 1)[-1] for method, url, _ in web.calls if "/job/" in url] == [
        "Account-Executive---SLED---NY-NJ_JR2140-1"]
    assert run.boards[0].requests == 4
    rows = stored(conn)
    assert sorted(k for k, r in rows.items() if r["description"]) == sorted([PM_ID, SAVED_ID])
    assert rows[PM_ID]["location"] == "US Remote; New York, NY"     # not replaced by "(+1 more)"
    assert all(r["closed_at"] is None and r["missed_fetches"] == 0 for r in rows.values())

    # Without a ranking config, nothing more is fetched: no title is wanted.
    web.calls.clear()
    scout.run_scout(conn, adapter_for=lambda source: adapter)
    assert len(web.calls) == 3


def test_a_partial_fetch_closes_nothing(conn):
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "workday", "acme/wd5/Jobs")
    full = quiet(board_of(60))
    scout.run_scout(conn, adapter_for=lambda source: full)
    partial = quiet(board_of(60), max_requests=2)            # robots and one page: 20 of 60
    for _ in range(3):
        run = scout.run_scout(conn, adapter_for=lambda source: partial)
    (board,) = run.boards
    assert (board.status, board.roles_fetched, board.complete) == ("ok", 20, False)
    assert "stopped early" in board.note
    assert run.snapshots[0].open_roles == 60 and run.snapshots[0].closed == 0
    assert conn.execute("SELECT MAX(missed_fetches) FROM roles").fetchone()[0] == 0


def test_a_first_fetch_cut_short_is_not_the_baseline(conn):
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "workday", "acme/wd5/Jobs")
    partial = quiet(board_of(60), max_requests=2)
    run = scout.run_scout(conn, adapter_for=lambda source: partial)
    assert (run.boards[0].baseline, run.boards[0].complete, run.snapshots[0].open_roles) == (True, False, 20)
    assert conn.execute("SELECT last_success_at FROM boards").fetchone()[0] is None
    # The complete fetch that follows is the baseline: its 40 further roles are not "opened".
    full = quiet(board_of(60))
    run = scout.run_scout(conn, adapter_for=lambda source: full)
    assert (run.boards[0].baseline, run.snapshots[0].open_roles, run.snapshots[0].opened) == (True, 60, 0)
    assert conn.execute("SELECT last_success_at FROM boards").fetchone()[0] is not None
    run = scout.run_scout(conn, adapter_for=lambda source: quiet(board_of(61)))
    assert (run.boards[0].baseline, run.snapshots[0].opened) == (False, 1)


def test_scout_command_reports_requests_and_time_per_board(conn, monkeypatch, capsys, tmp_path):
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "workday", BOARD)
    config = tmp_path / "ranking.toml"
    config.write_text('[title.wanted]\n"Product Manager III" = 30\n', encoding="utf-8")
    monkeypatch.setenv("ROLESCOUT_RANKING", str(config))
    adapter = Replay("workday_paging.json").adapter()
    real = scout.run_scout
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: real(c, adapter_for=lambda s: adapter, **kw))
    assert cli.main(["scout"]) == 0
    out = capsys.readouterr().out
    assert "Acme (workday/acme/wd5/Careers) ... 23 roles (baseline), 4 requests, 0s" in out
    assert "Requests" in out and "Time" in out
    assert conn.execute("SELECT COUNT(*) FROM role_descriptions").fetchone()[0] == 1


# --- in companies resolve --------------------------------------------------------

def first_page(total):
    return {"total": total, "jobPostings": [posting(n) for n in range(min(total, 20))]}


def resolve_one(conn, name, website, pages):
    companies.add_company(conn, name, website=website)
    web = FakeWeb(pages)
    (res,) = resolve_companies(conn, names=[name], resolver=Resolver(session=web))
    boards = [(b["source"], b["ref"], b["origin"]) for c in companies.list_companies(conn)
              if c["name"] == name for b in c["boards"]]
    return res, boards, web


def test_resolve_maps_a_company_from_a_workday_job_link(conn):
    res, boards, web = resolve_one(conn, "Acme Legal", "https://acmelegal.com", {
        "https://acmelegal.com": '<a href="https://acmelegal.com/careers">Careers</a>',
        "https://acmelegal.com/careers":
            '<a href="https://parentco.wd3.myworkdayjobs.com/en-US/AcmeLegal/job/Dayton-OH/X_R1/apply">Apply</a>',
        "https://parentco.wd3.myworkdayjobs.com/wday/cxs/parentco/AcmeLegal/jobs": first_page(253),
    })
    assert res.outcome == "mapped"
    assert boards == [("workday", "parentco/wd3/AcmeLegal", "link")]
    (cand,) = res.candidates
    assert (cand.roles, cand.detail) == (253, "linked from https://acmelegal.com/careers")
    # No slug was guessed on any source: a link settles it.
    assert not any("greenhouse" in u or "lever" in u or "ashby" in u for u in web.urls)


def test_resolve_stores_the_tenant_spelling_that_answers(conn):
    res, boards, _ = resolve_one(conn, "Acme Corp", "https://acmecorp.com", {
        "https://acmecorp.com": '<a href="https://acme-corp.wd5.myworkdayjobs.com/Careers">Jobs</a>',
        "https://acme-corp.wd5.myworkdayjobs.com/wday/cxs/acme-corp/Careers/jobs": 422,
        "https://acme-corp.wd5.myworkdayjobs.com/wday/cxs/acme_corp/Careers/jobs": first_page(22),
    })
    assert boards == [("workday", "acme_corp/wd5/Careers", "link")]


def test_a_page_that_only_mentions_workday_is_not_evidence(conn):
    """A homepage that contains the text "myworkdayjobs.com" and no Workday URL."""
    res, boards, _ = resolve_one(conn, "Globex", "https://globex.com", {
        "https://globex.com": '<script>var allowed = ["myworkdayjobs.com", "greenhouse.example"];</script>'
                            '<a href="https://www.globex.com/?to=myworkdayjobs.com">x</a>'
                            '<a href="https://acme.wd5.myworkdayjobs.com.evil.com/Jobs">y</a>',
    })
    assert boards == []
    assert (res.outcome, res.job_system, res.job_system_url) == ("unresolved", None, None)
    saved = conn.execute("SELECT job_system, job_system_url FROM company_resolutions").fetchone()
    assert tuple(saved) == (None, None)


def test_a_workday_host_without_a_site_is_recorded_with_the_workday_url(conn):
    res, boards, _ = resolve_one(conn, "Acme", "https://acme.com", {
        "https://acme.com": '<a href="https://acme.wd5.myworkdayjobs.com">Jobs</a>',
    })
    assert boards == []
    assert (res.outcome, res.job_system) == ("needs_review", "workday")
    assert res.job_system_url == "https://acme.wd5.myworkdayjobs.com"      # never the page it was on
    assert 'rolescout companies map "Acme" workday CAREER-SITE-URL' in res.notes[-1]


def test_a_linked_workday_board_that_does_not_exist_is_not_saved(conn):
    res, boards, _ = resolve_one(conn, "Acme", "https://acme.com", {
        "https://acme.com": '<a href="https://acme.wd5.myworkdayjobs.com/Gone">Jobs</a>',
    })
    assert boards == []
    assert any("but that board does not exist" in note for note in res.notes)


def test_upgrade_drops_workday_findings_that_are_not_workday_urls(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", 5)
    old = db.connect(path)
    for name in ("Mention", "Real", "Hand"):
        old.execute("INSERT INTO companies (name, created_at) VALUES (?, 'x')", (name,))
    insert = ("INSERT INTO company_resolutions (company_id, resolved_at, outcome, job_system, "
              "job_system_url, evidence, manual) VALUES (?, 'x', 'unsupported', 'workday', ?, '{}', ?)")
    old.execute(insert, (1, "https://www.example.com/", 0))
    old.execute(insert, (2, "https://acme.wd5.myworkdayjobs.com/Careers", 0))
    old.execute(insert, (3, "https://hand.example/careers", 1))       # recorded by hand: left alone
    old.close()
    monkeypatch.undo()

    conn = db.connect(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION
    kept = [r[0] for r in conn.execute("SELECT company_id FROM company_resolutions ORDER BY 1")]
    assert kept == [2, 3]
    assert "requests" in [r["name"] for r in conn.execute("PRAGMA table_info(run_boards)")]


def test_map_and_unmap_accept_a_url_or_a_reference(conn, capsys):
    companies.add_company(conn, "Globex")
    assert cli.main(["companies", "map", "Globex", "workday",
                     "https://globex.wd5.myworkdayjobs.com/en-US/2/userHome"]) == 0
    assert "Globex -> workday/globex/wd5/2" in capsys.readouterr().out
    assert cli.main(["companies", "map", "Globex", "workday", "https://www.globex.com/careers"]) == 1
    assert "is not a Workday board" in capsys.readouterr().err
    assert cli.main(["companies", "unmap", "Globex", "workday", "https://globex.wd5.myworkdayjobs.com/2"]) == 0
    assert "Removed workday/globex/wd5/2 from Globex" in capsys.readouterr().out
