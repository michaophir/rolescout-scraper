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

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
    session = fake_session(load_fixture("greenhouse_anthropic.json"))
    roles = GreenhouseAdapter(session).fetch("anthropic")

    assert session.urls == [
        "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true"]
    check_common(roles, "greenhouse")
    first = roles[0]
    assert first.source_job_id == "5456619008"
    assert first.company == "Anthropic"  # from the source's company_name
    assert first.title == "3P Revenue Accounting & Reporting"
    assert first.department == "Finance"
    assert first.location == "San Francisco, CA | Seattle, WA"
    assert first.workplace_type == ""
    assert first.url == "https://job-boards.greenhouse.io/anthropic/jobs/5456619008"
    assert first.date_posted == "2026-10-19"
    # first_published, not updated_at (2026-08-21 for this role)
    assert roles[1].date_posted == "2024-12-20"
    assert first.compensation_text == "$190,000 — $230,000 USD"
    assert first.description.startswith("About Anthropic Anthropic’s mission is")
    assert len(first.description) == 8864  # not truncated
    assert roles[2].title == "Account Executive, Nonprofit"  # trailing space stripped


def test_lever(load_fixture, fake_session):
    session = fake_session(load_fixture("lever_spotify.json"))
    roles = LeverAdapter(session).fetch("spotify")

    assert session.urls == ["https://api.lever.co/v0/postings/spotify?mode=json"]
    check_common(roles, "lever")
    first = roles[0]
    assert first.source_job_id == "2a55d6c9-c564-443a-be3e-a3851db5a93b"
    assert first.company == "spotify"  # Lever reports no company name
    assert first.title == "Account Executive - Commercial, Spotify Technology"
    assert first.department == "Operations and Business Support"
    assert first.location == "New York, NY"
    assert first.workplace_type == "hybrid"
    assert first.url == "https://jobs.lever.co/spotify/2a55d6c9-c564-443a-be3e-a3851db5a93b"
    assert first.date_posted == "2026-10-23"
    assert first.compensation_text.startswith(
        "The United States base range for this position is $87,069-$124,385 plus equity.")
    # Opening text, then the list sections under their headings, then `additional`.
    description = first.description
    assert description.startswith("Spotify Technology is our business-to-business")
    opening = description.index("clear business value will be key to your success.")
    do = description.index("What You'll Do\n• Identify and pursue new business opportunities")
    are = description.index("Who You Are\n• You have 4+ years of experience")
    where = description.index("Where You'll Be\nThis role is based in New York City")
    additional = description.index("The United States base range for this position")
    assert opening < do < are < where < additional
    assert "<" not in description
    assert roles[1].location == "London"


def test_ashby(load_fixture, fake_session):
    session = fake_session(load_fixture("ashby_ramp.json"))
    roles = AshbyAdapter(session).fetch("ramp")

    assert session.urls == [
        "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true"]
    check_common(roles, "ashby")
    first = roles[0]
    assert first.source_job_id == "34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert first.company == "ramp"  # Ashby reports no company name
    assert first.title == "Security Engineer, Cloud"  # leading space stripped
    assert first.department == "Engineering"
    assert first.location == "New York, NY (HQ)"
    assert first.workplace_type == "hybrid"
    assert first.url == "https://jobs.ashbyhq.com/ramp/34413f8d-26bf-4bbc-8ade-eb309a0e2245"
    assert first.date_posted == "2026-04-07"
    assert first.compensation_text == "$211.4K – $290.6K • Offers Equity"
    assert first.description.startswith("About Ramp Ramp is building")
    assert roles[2].department == "Sales"


@pytest.mark.parametrize("adapter_cls", ADAPTERS.values())
def test_company_override(adapter_cls, load_fixture, fake_session):
    name = {"greenhouse": "greenhouse_anthropic.json", "lever": "lever_spotify.json",
            "ashby": "ashby_ramp.json"}[adapter_cls.name]
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


@pytest.mark.parametrize("adapter_cls", ADAPTERS.values())
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
    assert set(ADAPTERS) == {"greenhouse", "lever", "ashby"}
    assert isinstance(get_adapter("lever"), LeverAdapter)
    with pytest.raises(ValueError):
        get_adapter("workday")
