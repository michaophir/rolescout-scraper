"""Rippling, Gem and Workable: references from URLs, the adapters against
recorded responses, and how ``companies resolve`` maps them. No network."""

import json
import subprocess
import sys

import pytest

from rolescout import cli, companies, scout
from rolescout.adapters import BoardNotFound, FetchContext, KnownRole, SourceError
from rolescout.adapters import gem, rippling, workable
from rolescout.adapters.gem import GemAdapter
from rolescout.adapters.rippling import RipplingAdapter
from rolescout.adapters.workable import WorkableAdapter
from rolescout.db import transaction
from rolescout.resolve import (
    Resolver, recheck_targets, resolve_companies, scan_for_boards, set_job_system,
)
from tests.conftest import FakeWeb

RIPPLING_LIST = "https://ats.rippling.com/api/v2/board/{}/jobs?page={}&pageSize=1000"
RIPPLING_JOB = "https://ats.rippling.com/{}/jobs/{}"
GEM_LIST = "https://api.gem.com/job_board/v0/{}/job_posts/"
WORKABLE_LIST = "https://apply.workable.com/api/v1/widget/accounts/{}?details=true"
WORKABLE_ACCOUNT = "https://apply.workable.com/api/v1/accounts/{}"

BIZOPS = "c57658c2-915c-e23d-75c4-95ca8fe34757"      # open in two places, both on site
PROGRAM = "b8b73f2b-0e4c-ff5f-2efc-9d2abc39855f"     # remote in one place, on site in two
HR = "fdbf61fe-ea29-efc6-d6bc-0034f467eba4"          # remote
PAYROLL = "5f00367c-c8a7-3d26-9bfd-8b7f69bd2247"     # hybrid


def next_page(data):
    return ('<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(data)
            + "</script></html>")


def wants_manager(title):
    return "Manager" in title and "Program" not in title and "für" not in title


# --- the board reference from every URL shape ---------------------------------

@pytest.mark.parametrize("url, ref", [
    ("https://ats.rippling.com/acme/jobs", "acme"),
    ("https://ats.rippling.com/en-GB/acme-inc/jobs", "acme-inc"),
    ("https://ats.rippling.com/acme-careers/jobs/6d965f8d-f038-4092-b32e-ff5e4dc0fca6", "acme-careers"),
    ("https://ats.rippling.com/en-GB/acme/jobs/2d72e861-ef93-4ea1-a585-4ef7f3241f20", "acme"),
    ("https://ats.rippling.com/embed/acme/jobs?s=https%3A%2F%2Fwww.acme.example%2Fabout", "acme"),
    ("https://ats.rippling.com/api/v2/board/acme/jobs?groupJobsByLocation=false&page=0&pageSize=1000", "acme"),
    ("ats.rippling.com/Acme/jobs", "acme"),
    ("https://ats.rippling.com/acme", "acme"),
])
def test_rippling_reference_from_every_url_shape(url, ref):
    assert rippling.parse_rippling_url(url) == ref
    assert RipplingAdapter.canonical_ref(url) == ref
    assert RipplingAdapter.canonical_ref(ref) == ref


@pytest.mark.parametrize("url, ref", [
    ("https://jobs.gem.com/acme", "acme"),
    ("https://jobs.gem.com/acme/", "acme"),
    ("https://jobs.gem.com/acme/4003629005", "acme"),
    ("https://jobs.gem.com/acme/ITEM_ID?embed=true", "acme"),
    ("https://jobs.gem.com/acme/am9icG9zdDqBgAV6R5An0LWf8Ua8v-s_", "acme"),
    ("https://jobs.gem.com/acme-ai/am9icG9zdDoJ5w5tKQWJncZvso2_ZzyH?utm_source=GemLink&utm_medium=c291cmNl", "acme-ai"),
    ("https://jobs.gem.com/acme/37c9854b-f1ea-4da6-a3f5-51e64923ae08", "acme"),
    ("https://api.gem.com/job_board/v0/acme-llc/job_posts/", "acme-llc"),
    ("jobs.gem.com/Acme", "acme"),
])
def test_gem_reference_from_every_url_shape(url, ref):
    assert gem.parse_gem_url(url) == ref
    assert GemAdapter.canonical_ref(url) == ref
    assert GemAdapter.canonical_ref(ref) == ref


@pytest.mark.parametrize("url, ref", [
    ("https://apply.workable.com/acme/", "acme"),
    ("https://apply.workable.com/acme/?lng=en", "acme"),
    ("https://apply.workable.com/acme-1/", "acme-1"),
    ("https://apply.workable.com/8a/j/6A2E99E4BE/", "8a"),
    ("https://apply.workable.com/acme/j/0B2B858E92/apply/", "acme"),
    ("https://acme-labs.workable.com/", "acme-labs"),
    ("https://acme.workable.com/jobs/4610985", "acme"),
    ("https://apply.workable.com/api/v1/widget/accounts/acme?details=true", "acme"),
    ("https://apply.workable.com/api/v3/accounts/acme/jobs", "acme"),
    ("https://www.workable.com/api/accounts/100001?details=true", "100001"),
])
def test_workable_reference_from_every_url_shape(url, ref):
    assert workable.parse_workable_url(url) == ref
    assert WorkableAdapter.canonical_ref(url) == ref
    assert WorkableAdapter.canonical_ref(ref) == ref


@pytest.mark.parametrize("parse, url", [
    (rippling.parse_rippling_url, "https://ats.rippling.com/"),
    (rippling.parse_rippling_url, "https://ats.rippling.com/api/v2/board"),
    (rippling.parse_rippling_url, "https://ats.rippling.com/_next/static/chunks/main.js"),
    (rippling.parse_rippling_url, "https://www.rippling.com/careers/open-roles"),
    (rippling.parse_rippling_url, "https://ats.rippling.com.evil.example/acme/jobs"),
    (gem.parse_gem_url, "https://jobs.gem.com/"),
    (gem.parse_gem_url, "https://www.gem.com/customers/acme"),
    (gem.parse_gem_url, "https://api.gem.com/v0/users"),
    (gem.parse_gem_url, "https://static.gem.com/scripts/jobBoards.v2.min.js"),
    (workable.parse_workable_url, "https://apply.workable.com/j/6A2E99E4BE"),     # a job, no account
    (workable.parse_workable_url, "https://apply.workable.com/"),
    (workable.parse_workable_url, "https://www.workable.com/assets/embed.js"),
    (workable.parse_workable_url, "https://help.workable.com/hc/en-us"),
    (workable.parse_workable_url, "https://resources.workable.com/product-manager-job-description"),
    (workable.parse_workable_url, "https://notworkable.com/acme"),
])
def test_urls_that_name_no_board(parse, url):
    assert parse(url) is None


def test_a_reference_that_is_neither_a_name_nor_a_board_url_is_refused():
    for adapter, text, system in ((RipplingAdapter, "https://www.acme.example/careers", "Rippling"),
                                  (GemAdapter, "https://www.acme.example/careers", "Gem"),
                                  (WorkableAdapter, "https://apply.workable.com/j/6A2E99E4BE", "Workable")):
        with pytest.raises(ValueError, match=f"is not a {system} board"):
            adapter.canonical_ref(text)


def test_boards_are_found_in_links_embeds_and_escaped_json():
    page = r'''
        <a href="https://ats.rippling.com/en-GB/acme-inc/jobs/2d72e861-ef93-4ea1-a585-4ef7f3241f20">Apply</a>
        <iframe src="https://ats.rippling.com/embed/acme-inc/jobs?s=https%3A%2F%2Fwww.acme.example"></iframe>
        <script>{"feed":"https:\/\/api.gem.com\/job_board\/v0\/acme\/job_posts\/"}</script>
        <a href="https://jobs.gem.com/acme/am9icG9zdDqBgAV6R5An0LWf8Ua8v-s_?embed=true">Role</a>
        <a href="https://acme-labs.workable.com/jobs/4610985">Role</a>
        <a href="https://www.workable.com/">Powered by Workable</a>
        <script src="https://static.gem.com/scripts/jobBoards.v2.min.js"></script>
    '''
    supported, unsupported = scan_for_boards(page)
    assert set(supported) == {("rippling", "acme-inc"), ("gem", "acme"), ("workable", "acme-labs")}
    assert unsupported == {}
    # The careers-page embed names the account by number; a URL, when there is one, wins.
    embed = '''<script src="https://www.workable.com/assets/embed.js"></script>
               <script>whr(document).ready(function(){ whr_embed(100001, {detail: 'titles'}); });</script>'''
    assert workable.find_refs(embed) == ["100001"]
    assert workable.find_refs(embed + '<a href="https://apply.workable.com/acme/">Jobs</a>') == ["acme"]


# --- rippling ------------------------------------------------------------------

def rippling_web(load_fixture, **extra):
    listing = load_fixture("rippling_role_listed_once_per_location.json")
    job = load_fixture("rippling_job_page_with_description_date_and_pay.json")
    return FakeWeb({
        RIPPLING_LIST.format("acme", 0): listing,
        RIPPLING_JOB.format("acme", BIZOPS): next_page(job),
        "https://ats.rippling.com/acme/jobs":
            next_page(load_fixture("rippling_board_page_names_the_company.json")),
        "https://ats.rippling.com/robots.txt": "User-agent: *\nDisallow: /internal/\n",
        **extra})


def test_rippling_lists_each_role_once_with_all_its_locations(load_fixture):
    web = rippling_web(load_fixture)
    adapter = RipplingAdapter(web)
    roles = {r.source_job_id: r for r in adapter.fetch("acme", company="Acme")}
    assert len(roles) == 4                                           # seven list entries, four jobs
    bizops = roles[BIZOPS]
    assert (bizops.source, bizops.company, bizops.title, bizops.department) == (
        "rippling", "Acme", "Business Operations Manager", "Bizops")
    assert (bizops.location, bizops.workplace_type) == ("San Francisco, CA; New York, NY", "onsite")
    assert bizops.url == RIPPLING_JOB.format("acme", BIZOPS)
    assert (bizops.date_posted, bizops.description) == ("", "")      # the list has neither
    # Remote in one place and on site in two: the locations say which, the role says neither.
    assert (roles[PROGRAM].location, roles[PROGRAM].workplace_type) == (
        "Remote (United States); San Francisco, CA; New York, NY", "")
    assert (roles[HR].location, roles[HR].workplace_type) == ("Philippines", "remote")
    assert (roles[PAYROLL].location, roles[PAYROLL].workplace_type) == ("Berlin", "hybrid")
    # robots.txt, then the list: no job page is read unless a description is wanted.
    assert web.urls == ["https://ats.rippling.com/robots.txt", RIPPLING_LIST.format("acme", 0)]
    assert adapter.last_stats.requests == 2 and adapter.last_stats.complete


def test_rippling_reads_description_date_and_pay_only_for_wanted_roles(load_fixture):
    web = rippling_web(load_fixture)
    adapter = RipplingAdapter(web)
    roles = {r.source_job_id: r for r in adapter.fetch(
        "acme", context=FetchContext(wants_description=wants_manager))}
    wanted = roles[BIZOPS]
    assert wanted.description.startswith("About Acme Acme builds tools for small teams.")
    assert "Work with design & engineering" in wanted.description and "<" not in wanted.description
    assert wanted.date_posted == "2026-03-31"
    assert wanted.compensation_text == "USD 185,000 - 240,000 per year (US)"
    assert all(r.description == "" for job, r in roles.items() if job != BIZOPS)
    assert [u for u in web.urls if "/jobs/" in u] == [RIPPLING_JOB.format("acme", BIZOPS)]
    stats = adapter.last_stats
    assert (stats.requests, stats.descriptions_fetched, stats.descriptions_waiting) == (3, 1, 0)


def test_rippling_keeps_what_is_stored_and_reads_saved_roles(load_fixture):
    web = rippling_web(load_fixture, **{
        RIPPLING_JOB.format("acme", HR): next_page(
            load_fixture("rippling_job_page_with_description_date_and_pay.json"))})
    adapter = RipplingAdapter(web)
    known = {BIZOPS: KnownRole(date_posted="2026-03-31", has_description=True),
             HR: KnownRole(keep=True)}                               # saved, no description yet
    roles = {r.source_job_id: r for r in adapter.fetch(
        "acme", context=FetchContext(wants_description=wants_manager, known=known))}
    # Not read twice, and the date an earlier fetch read is kept.
    assert RIPPLING_JOB.format("acme", BIZOPS) not in web.urls
    assert (roles[BIZOPS].date_posted, roles[BIZOPS].description) == ("2026-03-31", "")
    # A saved role gets its description whatever its title.
    assert RIPPLING_JOB.format("acme", HR) in web.urls and roles[HR].description


def test_rippling_leaves_descriptions_over_the_limit_for_the_next_run(load_fixture):
    web = rippling_web(load_fixture)
    adapter = RipplingAdapter(web, max_descriptions=1)
    roles = adapter.fetch("acme", context=FetchContext(wants_description=lambda title: True))
    assert len(roles) == 4 and len([u for u in web.urls if "/jobs/" in u]) == 1
    assert adapter.last_stats.descriptions_waiting == 3
    assert adapter.last_stats.notes == ["3 description(s) left for the next run"]
    assert adapter.last_stats.complete                               # every role was still listed


def test_rippling_role_that_closed_before_its_page_was_read_is_kept_without_one(load_fixture):
    web = rippling_web(load_fixture)
    del web.pages[RIPPLING_JOB.format("acme", BIZOPS)]               # 404
    adapter = RipplingAdapter(web)
    roles = adapter.fetch("acme", context=FetchContext(wants_description=wants_manager))
    assert len(roles) == 4 and all(r.description == "" for r in roles)


def test_rippling_reads_every_page_of_a_long_list(load_fixture):
    listing = load_fixture("rippling_role_listed_once_per_location.json")
    first = {**listing, "items": listing["items"][:4], "totalPages": 2}
    second = {**listing, "items": listing["items"][4:], "page": 1, "totalPages": 2}
    web = FakeWeb({RIPPLING_LIST.format("acme", 0): first, RIPPLING_LIST.format("acme", 1): second})
    adapter = RipplingAdapter(web)
    assert len(adapter.fetch("acme")) == 4
    assert web.urls[1:] == [RIPPLING_LIST.format("acme", 0), RIPPLING_LIST.format("acme", 1)]


def test_rippling_identity_missing_board_and_robots(load_fixture):
    web = rippling_web(load_fixture)
    adapter = RipplingAdapter(web)
    assert adapter.confirm("https://ats.rippling.com/en-GB/acme/jobs") == ("acme", 4)
    identity = adapter.identity("acme")
    # The board lives on Rippling's own address, which says nothing about the company's site.
    assert (identity.name, identity.website) == ("Acme", "")
    with pytest.raises(BoardNotFound):
        adapter.fetch("nosuchboard")
    refused = RipplingAdapter(FakeWeb({
        "https://ats.rippling.com/robots.txt": "User-agent: *\nDisallow: /api/\n",
        RIPPLING_LIST.format("acme", 0): load_fixture("rippling_role_listed_once_per_location.json")}))
    with pytest.raises(SourceError, match="robots.txt on ats.rippling.com disallows /api/v2/board/acme/jobs"):
        refused.fetch("acme")
    assert RIPPLING_LIST.format("acme", 0) not in refused.session.urls


def test_scout_stores_rippling_roles_and_only_the_wanted_descriptions(conn, load_fixture):
    companies.add_company(conn, "Acme", tier=2)
    # Mapped by pasting a job's URL.
    companies.add_board(conn, "Acme", "rippling", RIPPLING_JOB.format("en-GB/acme", BIZOPS))
    assert conn.execute("SELECT ref FROM boards").fetchone()["ref"] == "acme"
    web = rippling_web(load_fixture)
    adapter = RipplingAdapter(web)
    run = scout.run_scout(conn, adapter_for=lambda source: adapter, wants_description=wants_manager)
    (board,) = run.boards
    assert (board.status, board.roles_fetched, board.requests, board.complete) == ("ok", 4, 3, True)
    rows = {r["source_job_id"]: dict(r) for r in conn.execute(
        "SELECT r.*, d.description FROM roles r LEFT JOIN role_descriptions d ON d.role_id = r.id")}
    assert [job for job, r in rows.items() if r["description"]] == [BIZOPS]
    assert rows[BIZOPS]["date_posted"] == "2026-03-31"
    # The next run reads no job page again and keeps the date.
    web.urls.clear()
    scout.run_scout(conn, adapter_for=lambda source: adapter, wants_description=wants_manager)
    assert web.urls == [RIPPLING_LIST.format("acme", 0)]
    again = conn.execute("SELECT date_posted FROM roles WHERE source_job_id = ?", (BIZOPS,)).fetchone()
    assert again["date_posted"] == "2026-03-31"


# --- gem -----------------------------------------------------------------------

def test_gem_returns_every_role_with_its_description_in_one_request(load_fixture):
    web = FakeWeb({GEM_LIST.format("acme"): load_fixture("gem_board_with_descriptions.json"),
                   "https://api.gem.com/robots.txt": 403})           # as observed: no rules published
    adapter = GemAdapter(web)
    roles = adapter.fetch("https://jobs.gem.com/acme/4003629005", company="Acme")
    assert [r.title for r in roles] == [
        "Technical Program Manager", "Senior AE (Director of Strategic Relationships)", "Senior Product Manager"]
    first, remote, _ = roles
    assert (first.source, first.company, first.department) == ("gem", "Acme", "Customer Experience")
    assert first.source_job_id == "am9icG9zdDfe7675f4d33fc99ac30c1a"
    assert first.url == "https://jobs.gem.com/acme/am9icG9zdDfe7675f4d33fc99ac30c1a"
    assert (first.location, first.workplace_type) == (
        "New York, United States; San Francisco, United States", "hybrid")
    assert first.date_posted == "2026-05-04"
    assert first.description.startswith("Acme builds tools for small teams.") and "<" not in first.description
    assert (remote.location, remote.workplace_type) == ("Remote", "remote")
    assert web.urls == ["https://api.gem.com/robots.txt", GEM_LIST.format("acme")]
    assert adapter.last_stats.requests == 2


def test_gem_identity_and_missing_board(load_fixture):
    web = FakeWeb({GEM_LIST.format("acme"): load_fixture("gem_board_with_descriptions.json"),
                   "https://jobs.gem.com/acme": "<html><head><title>Acme Careers - Gem</title></head></html>"})
    adapter = GemAdapter(web)
    assert adapter.confirm("https://jobs.gem.com/acme/ITEM_ID?embed=true") == ("acme", 3)
    assert adapter.identity("acme").name == "Acme"
    with pytest.raises(BoardNotFound):
        adapter.fetch("nosuchboard")
    with pytest.raises(SourceError, match="unexpected response shape"):
        GemAdapter(FakeWeb({GEM_LIST.format("acme"): {"jobs": []}})).fetch("acme")


# --- workable ------------------------------------------------------------------

def test_workable_lists_each_role_once_with_all_its_locations(load_fixture):
    web = FakeWeb({WORKABLE_LIST.format("acme"): load_fixture("workable_role_listed_once_per_location.json")})
    adapter = WorkableAdapter(web)
    roles = adapter.fetch("https://acme.workable.com/jobs/4610985")
    assert [r.source_job_id for r in roles] == ["AEBA7CB58F", "889B8306D7", "0D89B66DDF"]   # five entries
    analyst, manager, marketing = roles
    assert (analyst.source, analyst.company, analyst.title) == ("workable", "Acme", "(Senior) FP&A Analyst")
    assert (analyst.location, analyst.workplace_type) == ("Spain; Germany; Hungary", "remote")
    assert (analyst.url, analyst.date_posted) == ("https://apply.workable.com/j/AEBA7CB58F", "2026-11-15")
    assert analyst.description.startswith("Acme builds tools for small teams.") and "<" not in analyst.description
    assert (manager.location, manager.workplace_type, manager.department) == (
        "La Vergne, Tennessee, United States", "", "Fulfillment")
    assert marketing.department == ""                                # the feed says null
    assert web.urls == ["https://apply.workable.com/robots.txt", WORKABLE_LIST.format("acme")]
    assert adapter.last_stats.requests == 2


def test_workable_identity_numeric_account_and_missing_board(load_fixture):
    feed = load_fixture("workable_role_listed_once_per_location.json")
    web = FakeWeb({WORKABLE_LIST.format("acme"): feed, WORKABLE_LIST.format("100001"): feed,
                   WORKABLE_ACCOUNT.format("acme"): load_fixture("workable_account_names_the_company.json")})
    adapter = WorkableAdapter(web)
    assert adapter.confirm("https://apply.workable.com/acme/j/6A2E99E4BE/") == ("acme", 3)
    identity = adapter.identity("acme")
    assert (identity.name, identity.website) == ("Acme", "https://www.acme.example/")
    # The account number from a careers-page embed works as the reference too.
    assert len(adapter.fetch("100001")) == 3
    assert adapter.identity("100001").name == "Acme"                 # by number, the feed's own name
    with pytest.raises(BoardNotFound):
        adapter.fetch("nosuchboard")


# --- in companies resolve --------------------------------------------------------

def resolve_one(conn, name, website, pages, **kwargs):
    if not conn.execute("SELECT 1 FROM companies WHERE name = ?", (name,)).fetchone():
        companies.add_company(conn, name, website=website)
    web = FakeWeb(pages)
    (res,) = resolve_companies(conn, names=[name], resolver=Resolver(session=web), **kwargs)
    boards = [(b["source"], b["ref"], b["origin"]) for c in companies.list_companies(conn)
              if c["name"] == name for b in c["boards"]]
    return res, boards, web


@pytest.mark.parametrize("link, feed_url, fixture, mapped", [
    ("https://ats.rippling.com/en-GB/acme-inc/jobs/2d72e861-ef93-4ea1-a585-4ef7f3241f20",
     RIPPLING_LIST.format("acme-inc", 0), "rippling_role_listed_once_per_location.json", ("rippling", "acme-inc", 4)),
    ("https://jobs.gem.com/acme/am9icG9zdDqBgAV6R5An0LWf8Ua8v-s_",
     GEM_LIST.format("acme"), "gem_board_with_descriptions.json", ("gem", "acme", 3)),
    ("https://apply.workable.com/acme-1/j/6A2E99E4BE/",
     WORKABLE_LIST.format("acme-1"), "workable_role_listed_once_per_location.json", ("workable", "acme-1", 3)),
])
def test_resolve_maps_a_company_from_a_link_to_its_board(conn, load_fixture, link, feed_url, fixture, mapped):
    source, ref, roles = mapped
    res, boards, web = resolve_one(conn, "Acme", "https://acme.example", {
        "https://acme.example": '<a href="/careers">Careers</a>',
        "https://acme.example/careers": f'<a href="{link}">Open role</a>',
        feed_url: load_fixture(fixture),
    })
    assert res.outcome == "mapped" and boards == [(source, ref, "link")]
    (cand,) = res.candidates
    assert (cand.roles, cand.detail) == (roles, "linked from https://acme.example/careers")
    # A link settles it: no slug was guessed on any source.
    assert not any("greenhouse" in u or "lever" in u or "ashby" in u for u in web.urls)


def test_resolve_maps_a_workable_embed_by_its_account_number(conn, load_fixture):
    res, boards, _ = resolve_one(conn, "Rivet", "https://rivet.example", {
        "https://rivet.example": '<a href="/careers">Careers</a>',
        "https://rivet.example/careers": '''
            <div id="whr_embed_hook"></div>
            <script src="https://www.workable.com/assets/embed.js" type="text/javascript"></script>
            <script>whr(document).ready(function(){ whr_embed(100001, {detail: 'titles'}); });</script>''',
        WORKABLE_LIST.format("100001"): load_fixture("workable_role_listed_once_per_location.json"),
    })
    assert res.outcome == "mapped" and boards == [("workable", "100001", "link")]


def test_a_page_that_only_mentions_the_job_system_is_not_mapped(conn):
    res, boards, _ = resolve_one(conn, "Acme", "https://acme.example", {
        "https://acme.example": '<a href="/careers">Careers</a>',
        "https://acme.example/careers": '<script>var host = "ats.rippling.com";</script>',
    })
    assert boards == [] and (res.outcome, res.job_system) == ("needs_review", "rippling")
    assert 'map it by hand: rolescout companies map "Acme" rippling BOARD' in res.notes[-1]


def test_a_linked_board_that_does_not_exist_is_not_saved(conn):
    res, boards, _ = resolve_one(conn, "Acme", "https://acme.example", {
        "https://acme.example": '<a href="https://jobs.gem.com/acme-old">Jobs</a>',
    })
    assert boards == [] and res.outcome != "mapped"
    assert "https://acme.example links to gem/acme-old, but that board does not exist" in res.notes


def record_unsupported(conn, name, system, url, manual=0):
    company_id = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()["id"]
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO company_resolutions (company_id, resolved_at, outcome, job_system, "
            "job_system_url, evidence, manual) VALUES (?, '2026-11-01T00:00:00Z', 'unsupported', ?, ?, '{}', ?)",
            (company_id, system, url, manual))


def test_a_company_recorded_on_these_systems_before_maps_from_the_recorded_url(conn, load_fixture):
    """Found as "unsupported: rippling" by an earlier version: the URL it
    recorded then names the board, so the company's site is not read again."""
    companies.add_company(conn, "Acme", website="https://acme.example")
    record_unsupported(conn, "Acme", "rippling",
                       "https://ats.rippling.com/api/v2/board/acme/jobs?groupJobsByLocation=false&page=0&pageSize=1000")
    assert [t["name"] for t in recheck_targets(conn, [])] == ["Acme"]
    web = FakeWeb({RIPPLING_LIST.format("acme", 0): load_fixture("rippling_role_listed_once_per_location.json")})
    (res,) = resolve_companies(conn, resolver=Resolver(session=web))          # the unattended pass
    assert res.outcome == "mapped"
    assert res.candidates[0].detail.startswith("job system URL found earlier: https://ats.rippling.com/api/v2/board/acme/")
    assert not any("acme.example" in u for u in web.urls)
    saved = conn.execute("SELECT source, ref, origin FROM boards").fetchone()
    assert tuple(saved) == ("rippling", "acme", "link")
    assert conn.execute("SELECT outcome FROM company_resolutions").fetchone()["outcome"] == "mapped"


def test_a_recorded_url_that_names_no_board_falls_back_to_reading_the_site(conn, load_fixture):
    companies.add_company(conn, "Acme", website="https://acme.example")
    record_unsupported(conn, "Acme", "workable", "https://acme.example/careers")   # where the embed was seen
    web = FakeWeb({
        "https://acme.example": '<a href="https://apply.workable.com/acme/">Jobs</a>',
        WORKABLE_LIST.format("acme"): load_fixture("workable_role_listed_once_per_location.json")})
    (res,) = resolve_companies(conn, resolver=Resolver(session=web))
    assert res.outcome == "mapped" and res.candidates[0].detail == "linked from https://acme.example"


def test_a_system_recorded_by_hand_is_picked_up_once_it_has_an_adapter(conn, load_fixture):
    companies.add_company(conn, "Acme", website="https://acme.example")
    companies.add_company(conn, "Other", website="https://other.example")
    record_unsupported(conn, "Acme", "gem", "https://jobs.gem.com/acme", manual=1)
    set_job_system(conn, "Other", "icims", "https://careers-other.icims.com/jobs")
    # Still no adapter for the other one: it stays out of the unattended pass.
    assert [t["name"] for t in recheck_targets(conn, [])] == ["Acme"]
    web = FakeWeb({GEM_LIST.format("acme"): load_fixture("gem_board_with_descriptions.json")})
    (res,) = resolve_companies(conn, resolver=Resolver(session=web))
    assert res.outcome == "mapped"
    assert tuple(conn.execute("SELECT source, ref FROM boards").fetchone()) == ("gem", "acme")
    with pytest.raises(companies.CompanyError, match="gem is a supported source: map the board instead"):
        set_job_system(conn, "Other", "gem", "https://jobs.gem.com/other")


def test_map_and_unmap_accept_a_url_or_a_name(conn, capsys):
    companies.add_company(conn, "Globex")
    assert cli.main(["companies", "map", "Globex", "rippling",
                     "https://ats.rippling.com/embed/globex-inc/jobs?s=https%3A%2F%2Fglobex.example"]) == 0
    assert "Globex -> rippling/globex-inc" in capsys.readouterr().out
    assert cli.main(["companies", "map", "Globex", "gem", "https://jobs.gem.com/globex/4003629005"]) == 0
    assert "Globex -> gem/globex" in capsys.readouterr().out
    assert cli.main(["companies", "map", "Globex", "workable", "https://globex.workable.com/"]) == 0
    assert "Globex -> workable/globex" in capsys.readouterr().out
    assert cli.main(["companies", "map", "Globex", "gem", "https://www.globex.example/careers"]) == 1
    assert "is not a Gem board" in capsys.readouterr().err
    assert cli.main(["companies", "unmap", "Globex", "workable", "https://apply.workable.com/globex/"]) == 0
    assert "Removed workable/globex from Globex" in capsys.readouterr().out


# --- output piped into a command that stops reading ----------------------------------

def test_a_table_piped_into_head_ends_quietly(conn, isolated_db_path):
    with transaction(conn):
        conn.executemany(
            "INSERT INTO companies (name, website, tier, tags, active, created_at) "
            "VALUES (?, ?, 3, '[]', 1, '2026-01-01T00:00:00Z')",
            [(f"Company {n:05d}", f"https://company-{n}.example") for n in range(4000)])
    conn.close()
    for command in (["gaps"], ["companies", "list"]):
        proc = subprocess.Popen([sys.executable, "-m", "rolescout", *command, "--db", str(isolated_db_path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first = proc.stdout.readline()              # what head does: read a little, then leave
        proc.stdout.close()
        errors = proc.stderr.read().decode()
        proc.stderr.close()
        assert first and proc.wait(timeout=60) == 0, errors
        assert errors == ""


# --- a site that shows the system but names no board ----------------------------------

def test_gem_embed_script_is_not_a_board():
    page = '''<script>window.__gemJobBoardUrl = "https://jobs.gem.com/acme-llc"</script>
              <script src="https://jobs.gem.com/gem/embed"></script><div id="gem_job_board_embed"></div>'''
    assert gem.find_refs(page) == ["acme-llc"]
    assert gem.parse_gem_url("https://jobs.gem.com/gem/embed") is None
    assert gem.parse_gem_url("https://jobs.gem.com/gem") == "gem"            # the vendor's own board is still one


def test_a_workable_board_at_the_company_name_is_saved_when_it_declares_the_company_site(conn, load_fixture):
    """The site only loads the embed script (no account in the page): the
    board at the company's name is tried, on that system alone."""
    res, boards, web = resolve_one(conn, "Acme", "https://acme.example", {
        "https://acme.example": '<a href="/careers">Careers</a>',
        "https://acme.example/careers": '<script src="https://www.workable.com/assets/embed.js"></script>',
        WORKABLE_LIST.format("acme"): load_fixture("workable_role_listed_once_per_location.json"),
        WORKABLE_ACCOUNT.format("acme"): load_fixture("workable_account_names_the_company.json"),
    })
    assert res.outcome == "mapped" and boards == [("workable", "acme", "guess")]
    assert res.candidates[0].detail in ("3 of 3 postings reference acme.example",
                                        "board declares website https://www.acme.example/")
    assert not any("greenhouse" in u or "lever" in u or "ashby" in u or "gem.com" in u for u in web.urls)


def test_a_rippling_board_matched_by_name_alone_waits_for_confirmation(conn, load_fixture):
    """The site only preconnects to the job system. A board with the company's
    name exists, but nothing ties it to the company's domain: it is offered,
    not saved."""
    res, boards, _ = resolve_one(conn, "Acme", "https://acme.example", {
        "https://acme.example": '<a href="/careers">Careers</a>',
        "https://acme.example/careers": '<link rel="preconnect" href="https://ats.rippling.com">',
        RIPPLING_LIST.format("acme", 0): load_fixture("rippling_role_listed_once_per_location.json"),
        "https://ats.rippling.com/acme/jobs":
            next_page(load_fixture("rippling_board_page_names_the_company.json")),
    })
    assert boards == [] and res.outcome == "needs_review"
    (cand,) = res.candidates
    assert (cand.source, cand.ref, cand.kind, cand.status, cand.roles) == ("rippling", "acme", "guess", "unconfirmed", 4)
    assert cand.detail == "board is named 'Acme', but nothing ties it to acme.example"


def test_these_systems_are_never_guessed_for_a_site_that_shows_no_sign_of_them(conn):
    res, _, web = resolve_one(conn, "Acme", "https://acme.example", {
        "https://acme.example": "<p>We are hiring.</p>"})
    assert res.outcome == "unresolved"
    assert not any("rippling" in u or "gem.com" in u or "workable" in u for u in web.urls)
