import pytest

from rolescout import companies
from rolescout.query import query_roles
from rolescout.scout import apply_fetch
from rolescout.db import transaction
from tests.test_scout import role


@pytest.fixture
def populated(conn):
    ids = {}
    for name in ("Acme", "Bolt"):
        ids[name] = companies.add_company(conn, name)
    with transaction(conn):
        apply_fetch(conn, ids["Acme"], [
            role("A1", title="Senior Product Manager, Growth"),
            role("A2", title="Product Marketing Manager"),
            role("A3", title="Staff Engineer"),
        ], "2026-03-01T00:00:00Z")
        apply_fetch(conn, ids["Bolt"], [
            role("B1", title="product manager", source="lever"),
            role("B2", title="100% Remote_Role"),
        ], "2026-03-10T00:00:00Z")
    # A3 goes missing twice -> closed.
    keep = [role("A1", title="Senior Product Manager, Growth"), role("A2", title="Product Marketing Manager")]
    for day in ("05", "06"):
        with transaction(conn):
            apply_fetch(conn, ids["Acme"], keep, f"2026-03-{day}T00:00:00Z")
    return conn


def titles(rows):
    return [r["title"] for r in rows]


def test_default_is_open_roles_ordered_by_company_then_title(populated):
    rows = query_roles(populated)
    assert [(r["company"], r["title"]) for r in rows] == [
        ("Acme", "Product Marketing Manager"), ("Acme", "Senior Product Manager, Growth"),
        ("Bolt", "100% Remote_Role"), ("Bolt", "product manager")]
    assert {r["status"] for r in rows} == {"open"}
    assert "description" not in rows[0]


def test_title_phrase_is_case_insensitive_substring(populated):
    assert titles(query_roles(populated, title=["product manager"])) == [
        "Senior Product Manager, Growth", "product manager"]
    assert titles(query_roles(populated, title=["PRODUCT MANAGER", "marketing"])) == [
        "Product Marketing Manager", "Senior Product Manager, Growth", "product manager"]
    # LIKE wildcards in the phrase are literal.
    assert titles(query_roles(populated, title=["100%"])) == ["100% Remote_Role"]
    assert titles(query_roles(populated, title=["e_r"])) == ["100% Remote_Role"]
    assert query_roles(populated, title=["%"]) == query_roles(populated, title=["100%"])


def test_company_status_since_and_limit(populated):
    assert titles(query_roles(populated, company="bolt")) == ["100% Remote_Role", "product manager"]
    closed = query_roles(populated, status="closed")
    assert [(r["title"], r["status"], r["closed_at"]) for r in closed] == [
        ("Staff Engineer", "closed", "2026-03-06T00:00:00Z")]
    assert len(query_roles(populated, status="all")) == 5
    assert titles(query_roles(populated, since="2026-03-02")) == ["100% Remote_Role", "product manager"]
    assert len(query_roles(populated, since="2026-03-01", status="all")) == 5
    assert len(query_roles(populated, limit=1)) == 1
    assert titles(query_roles(populated, company="Acme", title=["manager"], since="2026-03-02")) == []
    with pytest.raises(ValueError):
        query_roles(populated, since="yesterday")
