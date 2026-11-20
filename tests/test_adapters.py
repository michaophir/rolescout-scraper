"""Offline adapter tests against recorded source responses.

Expected values belong to the roles recorded in tests/fixtures. If you re-run
tests/refresh_fixtures.py, update them to match.
"""

import re
from dataclasses import fields

import pytest

from rolescout.adapters import (
    ADAPTERS, AshbyAdapter, BoardNotFound, GreenhouseAdapter, LeverAdapter,
    SourceError, get_adapter,
)
from rolescout.models import Role
from rolescout.text import pay_text

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# The sources that return a whole board in one call. Workday pages, and has
# its own tests in test_workday.py.
ONE_CALL = [GreenhouseAdapter, LeverAdapter, AshbyAdapter]


def check_common(roles, source):
    assert len(roles) == 3
    assert len({r.source_job_id for r in roles}) == 3
    for role in roles:
        assert role.source == source
        for f in fields(Role):
            assert isinstance(getattr(role, f.name), str), f.name
        assert role.source_job_id and role.title and role.url.startswith("https://")
        assert role.title == role.title.strip()
        assert ISO_DATE.fullmatch(role.date_posted)
        assert role.workplace_type in {"", "remote", "hybrid", "onsite"}
        assert len(role.description) > 500
        assert "<" not in role.description[:200]  # tags stripped


def test_greenhouse(load_fixture, fake_session):
    session = fake_session(load_fixture("greenhouse_board_with_content_and_pay.json"))
    roles = GreenhouseAdapter(session).fetch("acme")

    assert session.urls == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"]
    check_common(roles, "greenhouse")
    first = roles[0]
    assert first.source_job_id == "5456619008"
    assert first.company == "Acme"  # from the source's company_name
    assert first.title == "3P Revenue Accounting & Reporting"
    assert first.department == "Finance"
    assert first.location == "San Francisco, CA | Seattle, WA"
    assert first.workplace_type == ""
    assert first.url == "https://job-boards.greenhouse.io/acme/jobs/5456619008"
    assert first.date_posted == "2026-10-19"
    # first_published, not updated_at (2026-08-21 for this role)
    assert roles[1].date_posted == "2024-12-20"
    assert first.compensation_text == "$190,000 — $230,000 USD"
    assert first.description.startswith("About Acme Acme’s mission is")
    assert len(first.description) == 8812  # not truncated
    assert roles[2].title == "Account Executive, Nonprofit"  # trailing space stripped


def test_lever(load_fixture, fake_session):
    session = fake_session(load_fixture("lever_postings_with_list_sections.json"))
    roles = LeverAdapter(session).fetch("acme")

    assert session.urls == ["https://api.lever.co/v0/postings/acme?mode=json"]
    check_common(roles, "lever")
    first = roles[0]
    assert first.source_job_id == "2a55d6c9-c564-443a-be3e-a3851db5a93b"
    assert first.company == "acme"  # Lever reports no company name
    assert first.title == "Account Executive - Commercial, Acme Technology"
    assert first.department == "Operations and Business Support"
    assert first.location == "New York, NY"
    assert first.workplace_type == "hybrid"
    assert first.url == "https://jobs.lever.co/acme/2a55d6c9-c564-443a-be3e-a3851db5a93b"
    assert first.date_posted == "2026-10-23"
    # These postings fill in neither pay field. The closing section names a
    # range in passing; it is prose and stays in the description.
    assert [r.compensation_text for r in roles] == ["", "", ""]
    # Opening text, then the list sections under their headings, then `additional`.
    description = first.description
    assert description.startswith("Acme Technology is our business-to-business")
    opening = description.index("clear business value will be key to your success.")
    do = description.index("What You'll Do\n• Identify and pursue new business opportunities")
    are = description.index("Who You Are\n• You have 4+ years of experience")
    where = description.index("Where You'll Be\nThis role is based in New York City")
    additional = description.index("The United States base range for this position")
    assert opening < do < are < where < additional
    assert "<" not in description
    assert roles[1].location == "London"


def test_ashby(load_fixture, fake_session):
    session = fake_session(load_fixture("ashby_board_with_compensation.json"))
    roles = AshbyAdapter(session).fetch("acme")

    assert session.urls == [
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"]
    check_common(roles, "ashby")
    first = roles[0]
    assert first.source_job_id == "34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert first.company == "acme"  # Ashby reports no company name
    assert first.title == "Security Engineer, Cloud"  # leading space stripped
    assert first.department == "Engineering"
    assert first.location == "New York, NY (HQ)"
    assert first.workplace_type == "hybrid"
    assert first.url == "https://jobs.ashbyhq.com/acme/34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert first.date_posted == "2026-04-07"
    assert first.compensation_text == "$211.4K – $290.6K • Offers Equity"
    assert first.description.startswith("About Acme Acme is building")
    assert roles[2].department == "Sales"


@pytest.mark.parametrize("adapter_cls", ONE_CALL)
def test_company_override(adapter_cls, load_fixture, fake_session):
    name = {"greenhouse": "greenhouse_board_with_content_and_pay.json", "lever": "lever_postings_with_list_sections.json",
            "ashby": "ashby_board_with_compensation.json"}[adapter_cls.name]
    roles = adapter_cls(fake_session(load_fixture(name))).fetch("slug", company="Acme Inc")
    assert {r.company for r in roles} == {"Acme Inc"}


def test_greenhouse_edge_cases(fake_session):
    payload = {"jobs": [{
        "id": 7, "title": "Engineer", "absolute_url": "https://x/7",
        "location": {"name": "Remote - US"}, "departments": [],
        "first_published": None, "updated_at": "2026-03-04T10:00:00-05:00",
        "content": "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;",
    }]}
    (role,) = GreenhouseAdapter(fake_session(payload)).fetch("acme")
    assert role.workplace_type == "remote"  # inferred from location
    assert role.company == "acme"  # falls back to the slug
    assert role.date_posted == "2026-03-04"  # falls back to updated_at
    assert (role.department, role.compensation_text) == ("", "")
    assert role.description == "Build & ship"  # double-encoded entities decoded


def test_lever_edge_cases(fake_session):
    payload = [{"id": "a", "text": "Engineer", "hostedUrl": "https://x/a",
                "description": "<p>Hello <b>there</b></p>", "workplaceType": "unspecified",
                "lists": [{"text": "Requirements", "content": "<li>Python</li><li>SQL &amp; dbt</li>"}],
                "additional": "<div>Pay: $1</div>",
                "createdAt": 1700000000000}]
    (role,) = LeverAdapter(fake_session(payload)).fetch("acme")
    # HTML fallbacks when the *Plain fields are absent; list items one per line.
    assert role.description == "Hello there\n\nRequirements\nPython\nSQL & dbt\n\nPay: $1"
    assert role.workplace_type == ""
    assert role.date_posted == "2023-11-14"  # UTC, independent of local timezone
    assert (role.department, role.location, role.compensation_text) == ("", "", "")


def test_lever_pay_comes_from_the_pay_fields_and_never_from_the_closing_section(load_fixture, fake_session):
    payload = load_fixture("lever_postings_with_pay_fields.json")
    both, range_only, neither = LeverAdapter(fake_session(payload)).fetch("acme")
    assert both.compensation_text == "USD 80,000 - 165,000 per year"
    assert range_only.compensation_text == "USD 90,000 - 110,000 per year"
    # No pay field: the closing section (benefits, then the equal-opportunity
    # statement) is not pay, though it is full of numbers.
    assert any(ch.isdigit() for ch in payload[2]["additionalPlain"])
    assert "without attention to race" in payload[2]["additionalPlain"]
    assert neither.compensation_text == ""
    assert "without attention to race" in neither.description   # still part of the description

    def pay(**fields):
        item = {"id": "a", "text": "Engineer", "hostedUrl": "https://x/a", "additionalPlain": "We pay $1.", **fields}
        return LeverAdapter(fake_session([item])).fetch("acme")[0].compensation_text

    assert pay(salaryRange={"min": 60, "max": 60, "currency": "EUR", "interval": "per-hour-wage"}) == "EUR 60 per hour"
    assert pay(salaryRange={"min": 5000, "interval": "one-time"}) == "5,000 one time"
    # A sentence about pay counts when it names a figure, and only then.
    assert pay(salaryDescriptionPlain="Base pay is\n$120,000 to $150,000.") == "Base pay is $120,000 to $150,000."
    assert pay(salaryDescription="<p>Up to &euro;90k</p>") == "Up to €90k"
    assert pay(salaryDescriptionPlain="Competitive salary and equity.") == ""
    assert pay(salaryRange={"min": None, "max": 0, "currency": "USD"}) == ""
    assert pay(salaryRange="n/a") == ""


def test_compensation_never_holds_text_without_a_figure(fake_session):
    assert pay_text(" $150K -\n $200K ") == "$150K - $200K"
    assert [pay_text(t) for t in ("Competitive salary", "Offers Equity", "", None)] == ["", "", "", ""]

    job = {"id": "a", "title": "Engineer", "descriptionHtml": "<p>Hi</p>"}
    summaries = ["Offers Equity", "$150K – $200K • Offers Equity", None]
    roles = AshbyAdapter(fake_session({"jobs": [
        {**job, "id": str(n), "compensation": {"compensationTierSummary": text}}
        for n, text in enumerate(summaries)]})).fetch("acme")
    assert [r.compensation_text for r in roles] == ["", "$150K – $200K • Offers Equity", ""]

    def greenhouse(content):
        payload = {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x/1", "content": content}]}
        return GreenhouseAdapter(fake_session(payload)).fetch("acme")[0].compensation_text

    assert greenhouse('<div class="pay-range"><span>$100,000</span><span>—</span><span>$120,000 USD</span></div>') \
        == "$100,000 — $120,000 USD"
    notice = '<div class="content-pay-transparency"><p>We pay fairly and review pay every year.</p></div>'
    assert greenhouse(notice) == ""
    # A range element with nothing in it does not hide the block that has the figure.
    assert greenhouse('<div class="pay-range"><span>TBD</span></div>'
                      '<div class="content-pay-transparency">Annual base: $90,000</div>') == "Annual base: $90,000"


@pytest.mark.parametrize("name", ["greenhouse_board_with_content_and_pay.json", "lever_postings_with_list_sections.json",
                                  "lever_postings_with_pay_fields.json", "ashby_board_with_compensation.json"])
def test_recorded_boards_give_pay_with_a_figure_or_none(name, load_fixture, fake_session):
    adapter = get_adapter(name.split("_")[0], fake_session(load_fixture(name)))
    for role in adapter.fetch("acme"):
        assert role.compensation_text == "" or any(ch.isdigit() for ch in role.compensation_text)
        assert len(role.compensation_text) <= 500


def test_ashby_edge_cases(fake_session):
    base = {"title": "Engineer", "descriptionHtml": "<p>Hi</p>", "publishedAt": None}
    payload = {"jobs": [
        {**base, "id": "a", "workplaceType": "OnSite", "compensation": None},
        {**base, "id": "b", "workplaceType": None, "isListed": True},
        {**base, "id": "c", "isListed": False},
    ]}
    roles = AshbyAdapter(fake_session(payload)).fetch("acme")
    assert [r.source_job_id for r in roles] == ["a", "b"]  # unlisted role dropped
    assert [r.workplace_type for r in roles] == ["onsite", ""]
    assert roles[0].url == "https://jobs.ashbyhq.com/acme/a"
    assert (roles[0].compensation_text, roles[0].date_posted) == ("", "")


@pytest.mark.parametrize("adapter_cls", ONE_CALL)
def test_errors_are_raised_not_swallowed(adapter_cls, fake_session):
    with pytest.raises(BoardNotFound):
        adapter_cls(fake_session(status_code=404)).fetch("nope")
    with pytest.raises(SourceError):
        adapter_cls(fake_session(status_code=500)).fetch("nope")
    with pytest.raises(SourceError):
        adapter_cls(fake_session("not the expected shape")).fetch("nope")


def test_empty_board_is_not_an_error(fake_session):
    assert GreenhouseAdapter(fake_session({"jobs": []})).fetch("acme") == []
    assert LeverAdapter(fake_session([])).fetch("acme") == []
    assert AshbyAdapter(fake_session({"jobs": []})).fetch("acme") == []


def test_get_adapter():
    assert set(ADAPTERS) == {"greenhouse", "lever", "ashby", "workday", "rippling", "gem", "workable", "eightfold", "google"}
    assert isinstance(get_adapter("lever"), LeverAdapter)
    with pytest.raises(ValueError):
        get_adapter("icims")
