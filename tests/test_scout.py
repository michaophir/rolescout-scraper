"""The role lifecycle, run against a temporary database with scripted fetches."""

import json

import pytest

from rolescout import companies
from rolescout.adapters import SourceError
from rolescout.models import Role
from rolescout.scout import run_scout


def role(job_id, title="Engineer", department="Eng", source="greenhouse", description=None):
    return Role(
        source=source, source_job_id=job_id, company="x", title=title, department=department,
        location="NYC", workplace_type="hybrid", url=f"https://example.com/{job_id}",
        date_posted="2026-01-01", compensation_text="$1",
        description=description or f"Description of {job_id}")


class Scripted:
    """Stands in for the adapters: each board serves a queue of results, one per run."""

    def __init__(self, **boards):
        self.boards = {ref: list(results) for ref, results in boards.items()}
        self.calls = []

    def __call__(self, source):
        return self

    def fetch(self, ref, *, company=None):
        self.calls.append(ref)
        result = self.boards[ref].pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class Clock:
    """Each run gets a distinct timestamp: T1 for run 1, T2 for run 2, ..."""

    def __init__(self):
        self.run = 0

    def start(self):
        self.run += 1
        return f"2026-01-{self.run:02d}T00:00:00Z"

    def __call__(self):
        return f"2026-01-{self.run:02d}T00:00:00Z"


@pytest.fixture
def acme(conn):
    companies.add_company(conn, "Acme")
    companies.set_mapping(conn, "Acme", "greenhouse", "acme")
    return conn


def scout(conn, script, clock):
    clock.start()
    return run_scout(conn, adapter_for=script, clock=clock)


def stored(conn, job_id):
    row = conn.execute(
        "SELECT r.*, d.description FROM roles r LEFT JOIN role_descriptions d ON d.role_id = r.id "
        "WHERE r.source_job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def snapshot(conn, run_id, company="Acme"):
    row = conn.execute(
        "SELECT s.open_roles, s.opened, s.closed, s.open_by_department FROM company_snapshots s "
        "JOIN companies c ON c.id = s.company_id WHERE s.run_id = ? AND c.name = ?",
        (run_id, company)).fetchone()
    return (row["open_roles"], row["opened"], row["closed"], json.loads(row["open_by_department"]))


def test_full_lifecycle(acme):
    conn, clock = acme, Clock()
    a, b = role("A", department="Eng"), role("B", department="Sales")
    script = Scripted(acme=[
        [a, b],                 # run 1: both new
        [a, b],                 # run 2: still open
        [a],                    # run 3: B missing once
        SourceError("boom"),    # run 4: failed fetch
        [a],                    # run 5: B missing twice -> closed
        [a, b],                 # run 6: B reappears -> reopened
    ])

    # Run 1 — new roles.
    run = scout(conn, script, clock)
    assert (run.status, run.ok, run.failed) == ("completed", 1, 0)
    assert snapshot(conn, 1) == (2, 2, 0, {"Eng": 1, "Sales": 1})
    b_row = stored(conn, "B")
    assert b_row["first_seen_at"] == b_row["last_seen_at"] == "2026-01-01T00:00:00Z"
    assert (b_row["closed_at"], b_row["description"]) == (None, "Description of B")
    assert (b_row["title"], b_row["department"], b_row["location"], b_row["workplace_type"],
            b_row["url"], b_row["date_posted"], b_row["compensation_text"]) == (
        "Engineer", "Sales", "NYC", "hybrid", "https://example.com/B", "2026-01-01", "$1")

    # Run 2 — still open: last_seen moves, first_seen does not.
    scout(conn, script, clock)
    assert snapshot(conn, 2) == (2, 0, 0, {"Eng": 1, "Sales": 1})
    b_row = stored(conn, "B")
    assert (b_row["first_seen_at"], b_row["last_seen_at"]) == (
        "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z")

    # Run 3 — missing once: stays open, description kept.
    scout(conn, script, clock)
    assert snapshot(conn, 3) == (2, 0, 0, {"Eng": 1, "Sales": 1})
    b_row = stored(conn, "B")
    assert (b_row["closed_at"], b_row["missed_fetches"]) == (None, 1)
    assert b_row["description"] == "Description of B"
    assert b_row["last_seen_at"] == "2026-01-02T00:00:00Z"

    # Run 4 — failed fetch: nothing closes, nothing is counted as a miss.
    run = scout(conn, script, clock)
    assert (run.ok, run.failed, run.companies[0].error) == (0, 1, "boom")
    assert snapshot(conn, 4) == (2, 0, 0, {"Eng": 1, "Sales": 1})
    assert stored(conn, "B")["missed_fetches"] == 1
    assert stored(conn, "A")["last_seen_at"] == "2026-01-03T00:00:00Z"
    mapping = conn.execute("SELECT * FROM company_sources").fetchone()
    assert (mapping["last_success_at"], mapping["last_error"], mapping["last_error_at"]) == (
        "2026-01-03T00:00:00Z", "boom", "2026-01-04T00:00:00Z")

    # Run 5 — missing from a second successful fetch: closed, description deleted, row kept.
    scout(conn, script, clock)
    assert snapshot(conn, 5) == (1, 0, 1, {"Eng": 1})
    b_row = stored(conn, "B")
    assert b_row["closed_at"] == "2026-01-05T00:00:00Z"
    assert b_row["description"] is None
    assert (b_row["title"], b_row["last_seen_at"]) == ("Engineer", "2026-01-02T00:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM role_descriptions").fetchone()[0] == 1
    mapping = conn.execute("SELECT * FROM company_sources").fetchone()
    assert (mapping["last_success_at"], mapping["last_error"]) == ("2026-01-05T00:00:00Z", None)

    # Run 6 — reappears: reopens with its original first_seen and a fresh description.
    scout(conn, script, clock)
    assert snapshot(conn, 6) == (2, 1, 0, {"Eng": 1, "Sales": 1})
    b_row = stored(conn, "B")
    assert (b_row["closed_at"], b_row["missed_fetches"]) == (None, 0)
    assert (b_row["first_seen_at"], b_row["last_seen_at"]) == (
        "2026-01-01T00:00:00Z", "2026-01-06T00:00:00Z")
    assert b_row["description"] == "Description of B"
    assert conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 2  # never duplicated

    # Every run is on record.
    runs = conn.execute(
        "SELECT status, companies_ok, companies_failed, roles_open, roles_opened, roles_closed "
        "FROM runs ORDER BY id").fetchall()
    assert [tuple(r) for r in runs] == [
        ("completed", 1, 0, 2, 2, 0), ("completed", 1, 0, 2, 0, 0), ("completed", 1, 0, 2, 0, 0),
        ("completed", 0, 1, 2, 0, 0), ("completed", 1, 0, 1, 0, 1), ("completed", 1, 0, 2, 1, 0)]
    results = conn.execute(
        "SELECT status, error, roles_fetched FROM run_companies ORDER BY run_id").fetchall()
    assert [tuple(r) for r in results][3:5] == [("error", "boom", None), ("ok", None, 1)]


def test_a_miss_is_forgiven_when_the_role_comes_back(acme):
    conn, clock = acme, Clock()
    a, b = role("A"), role("B")
    script = Scripted(acme=[[a, b], [a], [a, b], [a], [a]])
    for _ in range(4):
        scout(conn, script, clock)
    assert stored(conn, "B")["closed_at"] is None  # two misses, but not consecutive
    assert stored(conn, "B")["missed_fetches"] == 1
    scout(conn, script, clock)
    assert stored(conn, "B")["closed_at"] == "2026-01-05T00:00:00Z"


def test_empty_board_needs_two_fetches_to_close_everything(acme):
    conn, clock = acme, Clock()
    script = Scripted(acme=[[role("A"), role("B")], [], []])
    scout(conn, script, clock)
    scout(conn, script, clock)
    assert snapshot(conn, 2)[:3] == (2, 0, 0)
    scout(conn, script, clock)
    assert snapshot(conn, 3) == (0, 0, 2, {})
    assert conn.execute("SELECT COUNT(*) FROM role_descriptions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 2


def test_changed_fields_and_description_are_updated(acme):
    conn, clock = acme, Clock()
    script = Scripted(acme=[
        [role("A", title="Engineer", description="old")],
        [role("A", title="Senior Engineer", description="new")],
    ])
    scout(conn, script, clock)
    scout(conn, script, clock)
    row = stored(conn, "A")
    assert (row["title"], row["description"]) == ("Senior Engineer", "new")
    assert snapshot(conn, 2)[:3] == (1, 0, 0)


def test_one_failure_does_not_stop_the_run(conn):
    for name, ref in (("Acme", "acme"), ("Bolt", "bolt"), ("Cobalt", "cobalt")):
        companies.add_company(conn, name)
        companies.set_mapping(conn, name, "greenhouse", ref)
    script = Scripted(
        acme=[[role("A1")]], bolt=[RuntimeError("unexpected crash")], cobalt=[[role("C1"), role("C2")]])
    seen = []
    clock = Clock()
    clock.start()
    run = run_scout(conn, adapter_for=script, clock=clock, on_company=seen.append)

    assert script.calls == ["acme", "bolt", "cobalt"]
    assert [(c.company, c.status, c.roles_fetched) for c in run.companies] == [
        ("Acme", "ok", 1), ("Bolt", "error", None), ("Cobalt", "ok", 2)]
    assert seen == run.companies
    assert (run.status, run.ok, run.failed) == ("completed", 2, 1)
    assert run.companies[1].error == "unexpected crash"
    assert snapshot(conn, 1, "Bolt") == (0, 0, 0, {})
    assert snapshot(conn, 1, "Cobalt")[:3] == (2, 2, 0)


def test_only_active_mapped_companies_are_fetched(conn):
    companies.add_company(conn, "Mapped")
    companies.set_mapping(conn, "Mapped", "lever", "mapped")
    companies.add_company(conn, "Unmapped")
    companies.add_company(conn, "Inactive")
    companies.set_mapping(conn, "Inactive", "lever", "inactive")
    companies.set_active(conn, "Inactive", False)
    script = Scripted(mapped=[[role("M1", source="lever")]])
    clock = Clock()
    run = scout(conn, script, clock)
    assert script.calls == ["mapped"]
    assert run.unmapped == 1
    assert conn.execute("SELECT companies_total FROM runs").fetchone()[0] == 1


def test_interrupted_run_is_recorded_and_keeps_finished_companies(conn):
    for name, ref in (("Acme", "acme"), ("Bolt", "bolt")):
        companies.add_company(conn, name)
        companies.set_mapping(conn, name, "greenhouse", ref)
    script = Scripted(acme=[[role("A1")]], bolt=[KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        scout(conn, script, Clock())
    run = conn.execute("SELECT status, companies_ok, finished_at FROM runs").fetchone()
    assert (run["status"], run["companies_ok"]) == ("interrupted", 1)
    assert run["finished_at"]
    assert stored(conn, "A1") is not None
