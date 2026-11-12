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
    """Each run gets a distinct timestamp: 2026-01-01 for run 1, 01-02 for run 2, ..."""

    def __init__(self):
        self.run = 0

    def start(self):
        self.run += 1
        return self()

    def __call__(self):
        return f"2026-01-{self.run:02d}T00:00:00Z"


def day(n):
    return f"2026-01-{n:02d}T00:00:00Z"


@pytest.fixture
def acme(conn):
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "greenhouse", "acme")
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
    """(open, opened, closed, baseline) for a company in a run."""
    row = conn.execute(
        "SELECT s.* FROM company_snapshots s JOIN companies c ON c.id = s.company_id "
        "WHERE s.run_id = ? AND c.name = ?", (run_id, company)).fetchone()
    return (row["open_roles"], row["opened"], row["closed"], bool(row["baseline"]))


def departments(conn, run_id, company="Acme"):
    row = conn.execute(
        "SELECT s.open_by_department FROM company_snapshots s JOIN companies c "
        "ON c.id = s.company_id WHERE s.run_id = ? AND c.name = ?", (run_id, company)).fetchone()
    return json.loads(row["open_by_department"])


def test_full_lifecycle(acme):
    conn, clock = acme, Clock()
    a, b, c = role("A", department="Eng"), role("B", department="Sales"), role("C", department="Eng")
    script = Scripted(acme=[
        [a, b],                 # run 1: baseline
        [a, b, c],              # run 2: C is new
        [a, b, c],              # run 3: still open
        [a, c],                 # run 4: B missing once
        SourceError("boom"),    # run 5: failed fetch
        [a, c],                 # run 6: B missing twice -> closed
        [a, b, c],              # run 7: B reappears -> reopened
    ])

    # Run 1 — baseline: roles are stored with first-seen dates but not counted as opened.
    run = scout(conn, script, clock)
    assert (run.status, run.ok, run.failed) == ("completed", 1, 0)
    assert run.boards[0].baseline is True
    assert snapshot(conn, 1) == (2, 0, 0, True)
    assert departments(conn, 1) == {"Eng": 1, "Sales": 1}
    b_row = stored(conn, "B")
    assert b_row["first_seen_at"] == b_row["last_seen_at"] == day(1)
    assert (b_row["closed_at"], b_row["description"]) == (None, "Description of B")
    assert (b_row["title"], b_row["department"], b_row["location"], b_row["workplace_type"],
            b_row["url"], b_row["date_posted"], b_row["compensation_text"]) == (
        "Engineer", "Sales", "NYC", "hybrid", "https://example.com/B", "2026-01-01", "$1")

    # Run 2 — a new role after the baseline counts as opened.
    run = scout(conn, script, clock)
    assert run.boards[0].baseline is False
    assert snapshot(conn, 2) == (3, 1, 0, False)
    assert departments(conn, 2) == {"Eng": 2, "Sales": 1}
    assert stored(conn, "C")["first_seen_at"] == day(2)

    # Run 3 — still open: last_seen moves, first_seen does not.
    scout(conn, script, clock)
    assert snapshot(conn, 3) == (3, 0, 0, False)
    b_row = stored(conn, "B")
    assert (b_row["first_seen_at"], b_row["last_seen_at"]) == (day(1), day(3))

    # Run 4 — missing once: stays open, description kept.
    scout(conn, script, clock)
    assert snapshot(conn, 4) == (3, 0, 0, False)
    b_row = stored(conn, "B")
    assert (b_row["closed_at"], b_row["missed_fetches"]) == (None, 1)
    assert b_row["description"] == "Description of B"
    assert b_row["last_seen_at"] == day(3)

    # Run 5 — failed fetch: nothing closes, nothing is counted as a miss.
    run = scout(conn, script, clock)
    assert (run.ok, run.failed, run.boards[0].error) == (0, 1, "boom")
    assert snapshot(conn, 5) == (3, 0, 0, False)
    assert stored(conn, "B")["missed_fetches"] == 1
    assert stored(conn, "A")["last_seen_at"] == day(4)
    board = conn.execute("SELECT * FROM boards").fetchone()
    assert (board["last_success_at"], board["last_error"], board["last_error_at"]) == (
        day(4), "boom", day(5))

    # Run 6 — missing from a second successful fetch: closed, description deleted, row kept.
    scout(conn, script, clock)
    assert snapshot(conn, 6) == (2, 0, 1, False)
    assert departments(conn, 6) == {"Eng": 2}
    b_row = stored(conn, "B")
    assert b_row["closed_at"] == day(6)
    assert b_row["description"] is None
    assert (b_row["title"], b_row["last_seen_at"]) == ("Engineer", day(3))
    assert conn.execute("SELECT COUNT(*) FROM role_descriptions").fetchone()[0] == 2
    board = conn.execute("SELECT * FROM boards").fetchone()
    assert (board["last_success_at"], board["last_error"]) == (day(6), None)

    # Run 7 — reappears: reopens with its original first_seen and a fresh description.
    scout(conn, script, clock)
    assert snapshot(conn, 7) == (3, 1, 0, False)
    b_row = stored(conn, "B")
    assert (b_row["closed_at"], b_row["missed_fetches"]) == (None, 0)
    assert (b_row["first_seen_at"], b_row["last_seen_at"]) == (day(1), day(7))
    assert b_row["description"] == "Description of B"
    assert conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 3  # never duplicated

    # Every run is on record.
    runs = conn.execute(
        "SELECT status, boards_ok, boards_failed, roles_open, roles_opened, roles_closed "
        "FROM runs ORDER BY id").fetchall()
    assert [tuple(r) for r in runs] == [
        ("completed", 1, 0, 2, 0, 0), ("completed", 1, 0, 3, 1, 0), ("completed", 1, 0, 3, 0, 0),
        ("completed", 1, 0, 3, 0, 0), ("completed", 0, 1, 3, 0, 0), ("completed", 1, 0, 2, 0, 1),
        ("completed", 1, 0, 3, 1, 0)]
    results = conn.execute(
        "SELECT status, error, roles_fetched, baseline FROM run_boards ORDER BY run_id").fetchall()
    assert [tuple(r) for r in results][0] == ("ok", None, 2, 1)
    assert [tuple(r) for r in results][4:6] == [("error", "boom", None, 0), ("ok", None, 2, 0)]


def test_baseline_waits_for_the_first_successful_fetch(acme):
    conn, clock = acme, Clock()
    script = Scripted(acme=[SourceError("down"), [role("A")], [role("A"), role("B")]])
    scout(conn, script, clock)
    assert snapshot(conn, 1) == (0, 0, 0, False)  # a failure is not a baseline
    scout(conn, script, clock)
    assert snapshot(conn, 2) == (1, 0, 0, True)
    scout(conn, script, clock)
    assert snapshot(conn, 3) == (2, 1, 0, False)


def test_a_miss_is_forgiven_when_the_role_comes_back(acme):
    conn, clock = acme, Clock()
    a, b = role("A"), role("B")
    script = Scripted(acme=[[a, b], [a], [a, b], [a], [a]])
    for _ in range(4):
        scout(conn, script, clock)
    assert stored(conn, "B")["closed_at"] is None  # two misses, but not consecutive
    assert stored(conn, "B")["missed_fetches"] == 1
    scout(conn, script, clock)
    assert stored(conn, "B")["closed_at"] == day(5)


def test_empty_board_needs_two_fetches_to_close_everything(acme):
    conn, clock = acme, Clock()
    script = Scripted(acme=[[role("A"), role("B")], [], []])
    scout(conn, script, clock)
    scout(conn, script, clock)
    assert snapshot(conn, 2) == (2, 0, 0, False)
    scout(conn, script, clock)
    assert snapshot(conn, 3) == (0, 0, 2, False)
    assert departments(conn, 3) == {}
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
    assert snapshot(conn, 2) == (1, 0, 0, False)


def test_company_with_two_boards(conn):
    conn_clock = Clock()
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "greenhouse", "acme")
    companies.add_board(conn, "Acme", "lever", "acme-eu")
    eu1, eu2 = role("EU1", source="lever"), role("EU2", source="lever")
    script = Scripted(**{
        "acme":    [[role("A1")], [role("A1"), role("A2")], [role("A1"), role("A2")], [role("A1"), role("A2")]],
        "acme-eu": [[eu1, eu2],   SourceError("eu down"),   [eu1],                    [eu1]],
    })

    # Run 1 — both boards baseline; one snapshot for the company.
    run = scout(conn, script, conn_clock)
    assert [(b.ref, b.baseline) for b in run.boards] == [("acme", True), ("acme-eu", True)]
    assert snapshot(conn, 1) == (3, 0, 0, True)
    assert len(run.snapshots) == 1

    # Run 2 — one board fails: the other's new role still counts, nothing on the failed board moves.
    run = scout(conn, script, conn_clock)
    assert [(b.ref, b.status) for b in run.boards] == [("acme", "ok"), ("acme-eu", "error")]
    assert snapshot(conn, 2) == (4, 1, 0, False)
    assert stored(conn, "EU2")["missed_fetches"] == 0

    # Runs 3 and 4 — a role closes after two misses on its own board only.
    scout(conn, script, conn_clock)
    assert snapshot(conn, 3) == (4, 0, 0, False)
    scout(conn, script, conn_clock)
    assert snapshot(conn, 4) == (3, 0, 1, False)
    assert stored(conn, "EU2")["closed_at"] == day(4)
    assert stored(conn, "A2")["closed_at"] is None


def test_a_board_added_later_gets_its_own_baseline(acme):
    conn, clock = acme, Clock()
    script = Scripted(**{
        "acme": [[role("A1")], [role("A1"), role("A2")]],
        "acme-eu": [[role("EU1", source="lever"), role("EU2", source="lever")]],
    })
    scout(conn, script, clock)
    companies.add_board(conn, "Acme", "lever", "acme-eu")
    scout(conn, script, clock)
    # A2 is genuinely new (opened 1); the new board's two roles are baseline, not opened.
    assert snapshot(conn, 2) == (4, 1, 0, True)
    assert stored(conn, "EU1")["first_seen_at"] == day(2)


def test_one_failure_does_not_stop_the_run(conn):
    for name, ref in (("Acme", "acme"), ("Bolt", "bolt"), ("Cobalt", "cobalt")):
        companies.add_company(conn, name)
        companies.add_board(conn, name, "greenhouse", ref)
    script = Scripted(
        acme=[[role("A1")]], bolt=[RuntimeError("unexpected crash")], cobalt=[[role("C1"), role("C2")]])
    seen = []
    clock = Clock()
    clock.start()
    run = run_scout(conn, adapter_for=script, clock=clock, on_board=seen.append)

    assert script.calls == ["acme", "bolt", "cobalt"]
    assert [(b.company, b.status, b.roles_fetched) for b in run.boards] == [
        ("Acme", "ok", 1), ("Bolt", "error", None), ("Cobalt", "ok", 2)]
    assert seen == run.boards
    assert (run.status, run.ok, run.failed) == ("completed", 2, 1)
    assert run.boards[1].error == "unexpected crash"
    assert snapshot(conn, 1, "Bolt") == (0, 0, 0, False)
    assert snapshot(conn, 1, "Cobalt") == (2, 0, 0, True)


def test_only_active_mapped_companies_are_fetched(conn):
    companies.add_company(conn, "Mapped")
    companies.add_board(conn, "Mapped", "lever", "mapped")
    companies.add_company(conn, "Unmapped")
    companies.add_company(conn, "Inactive")
    companies.add_board(conn, "Inactive", "lever", "inactive")
    companies.set_active(conn, "Inactive", False)
    script = Scripted(mapped=[[role("M1", source="lever")]])
    run = scout(conn, script, Clock())
    assert script.calls == ["mapped"]
    assert run.unmapped == 1
    assert tuple(conn.execute("SELECT companies_total, boards_total FROM runs").fetchone()) == (1, 1)


def test_interrupted_run_is_recorded_and_keeps_finished_companies(conn):
    for name, ref in (("Acme", "acme"), ("Bolt", "bolt")):
        companies.add_company(conn, name)
        companies.add_board(conn, name, "greenhouse", ref)
    script = Scripted(acme=[[role("A1")]], bolt=[KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        scout(conn, script, Clock())
    run = conn.execute("SELECT status, boards_ok, finished_at FROM runs").fetchone()
    assert (run["status"], run["boards_ok"]) == ("interrupted", 1)
    assert run["finished_at"]
    assert stored(conn, "A1") is not None
