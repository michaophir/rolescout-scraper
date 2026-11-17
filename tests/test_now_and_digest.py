"""What is running right now (status, MCP health) and the digest of what changed."""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from rolescout import candidates as cands
from rolescout import cli, companies, digest, jobs, ops, portfolio, resolve, scout
from rolescout import shortlist as ranking
from rolescout.adapters import SourceError
from rolescout.db import transaction
from rolescout.portfolio import Fetcher
from tests.conftest import FakeWeb
from tests.test_candidates import A, CONFIG, board_pages, co
from tests.test_scout import Clock, Scripted
from tests.test_shortlist import SMALL_CONFIG, mk


@pytest.fixture(autouse=True)
def utc(monkeypatch, tmp_path):
    """Times are shown in local time; pin the zone so the expected text is fixed."""
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("HOME", str(tmp_path))      # no real launchd schedule is read
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def insert_job(conn, kind, *, pid=None, started="2026-03-10T07:00:00Z", updated="2026-03-10T07:10:00Z",
               done=0, total=None, unit="", step=""):
    with transaction(conn):
        conn.execute(
            "INSERT INTO jobs (kind, pid, started_at, updated_at, done, total, unit, step) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (kind, pid or 1, started, updated, done, total, unit, step))


NOW = datetime(2026, 3, 10, 7, 12, tzinfo=timezone.utc)


def now_line(conn, alive=lambda pid: True):
    return jobs.current(conn, NOW, alive)["line"]


# --- the line itself -----------------------------------------------------------

def test_idle_when_nothing_is_running(conn, capsys):
    assert jobs.current(conn) == {"running": [], "stopped": [], "line": "Idle"}
    cli.main(["status"])
    assert "Right now:      Idle\n" in capsys.readouterr().out


def test_progress_and_estimated_finish_come_from_the_pace_so_far(conn):
    # 100 of 300 boards in the first ten minutes: twenty more to go.
    insert_job(conn, "scout", done=100, total=300, unit="boards")
    state = jobs.current(conn, NOW, lambda pid: True)
    assert state["line"] == "scout, started 07:00, 100 of 300 boards, should finish about 07:30"
    assert state["running"][0]["eta"] == "2026-03-10T07:30:00Z"
    with transaction(conn):
        conn.execute("UPDATE jobs SET done = 0")
    assert now_line(conn) == "scout, started 07:00, 0 of 300 boards, no estimate yet"


def test_a_job_started_on_another_day_says_which(conn):
    insert_job(conn, "harvest", started="2026-03-09T23:50:00Z", done=1, total=4, unit="boards",
               step="Alpha Fund: 120 of 300 companies read")
    assert now_line(conn).startswith(
        "harvest, started Mon 9 Mar 23:50, 1 of 4 boards, Alpha Fund: 120 of 300 companies read, should finish about")


def test_the_daily_job_names_the_step_it_is_on(conn):
    insert_job(conn, "daily", pid=4242, step="backup")
    assert now_line(conn) == "daily job, started 07:00; now: backup"
    # The scout it runs is its own process's job: shown as the step, with its progress.
    insert_job(conn, "scout", pid=4242, started="2026-03-10T07:01:00Z", updated="2026-03-10T07:11:00Z",
               done=412, total=725, unit="boards")
    assert now_line(conn) == ("daily job, started 07:00; now: scout, started 07:01, "
                              "412 of 725 boards, should finish about 07:18")
    # A resolve started by hand in another terminal is a separate job.
    insert_job(conn, "resolve", pid=5555, done=3, total=40, unit="companies")
    assert ". Also: resolve, started 07:00, 3 of 40 companies, should finish about" in now_line(conn)


# --- each job type, seen while it runs --------------------------------------------

def test_a_scout_is_shown_while_it_runs_and_gone_when_it_ends(conn):
    for name in ("Acme", "Bolt", "Cobalt"):
        companies.add_company(conn, name)
        companies.add_board(conn, name, "greenhouse", name.lower())
    seen = []

    class Watching(Scripted):
        def fetch(self, ref, *, company=None):
            seen.append(jobs.current(conn))
            return super().fetch(ref, company=company)
    scout.run_scout(conn, adapter_for=Watching(acme=[[mk("a1", "PM")]], bolt=[[mk("b1", "PM")]],
                                               cobalt=[[mk("c1", "PM")]]))
    assert [(s["running"][0]["kind"], s["running"][0]["done"], s["running"][0]["total"]) for s in seen] == [
        ("scout", 0, 3), ("scout", 1, 3), ("scout", 2, 3)]
    assert " 2 of 3 boards" in seen[2]["line"] and seen[2]["line"].startswith("scout, started ")
    assert seen[2]["running"][0]["eta"]                       # an estimate once there is a pace
    assert jobs.current(conn)["line"] == "Idle"


def test_a_resolve_is_shown_while_it_runs(conn):
    for name in ("Acme", "Bolt"):
        companies.add_company(conn, name, website=f"https://{name.lower()}.example")
    seen = []
    resolve.resolve_companies(conn, resolver=resolve.Resolver(session=FakeWeb({})),
                              on_company=lambda r: seen.append(jobs.current(conn)))
    assert [(s["running"][0]["kind"], s["running"][0]["total"], s["running"][0]["unit"]) for s in seen] == [
        ("resolve", 2, "companies")] * 2
    assert jobs.current(conn)["line"] == "Idle"


def test_a_harvest_is_shown_while_it_runs(conn):
    portfolio.add_board(conn, A, "Alpha Fund")
    seen = []
    web = FakeWeb(board_pages(A, "Alpha", "1", [co("acme", "acme.example", id=1)]))
    real = web.get

    def watching(url, **kwargs):
        seen.append(jobs.current(conn))
        return real(url, **kwargs)
    web.get = watching
    portfolio.harvest(conn, cands.parse_config(CONFIG, "test"), fetcher=Fetcher(session=web))
    kinds = {(s["running"][0]["kind"], s["running"][0]["total"], s["running"][0]["unit"]) for s in seen}
    assert kinds == {("harvest", 1, "boards")}
    assert any("Alpha Fund: 0 of 1 companies read" in s["line"] for s in seen)
    assert jobs.current(conn)["line"] == "Idle"


def test_the_daily_job_is_shown_with_the_scout_inside_it(conn, monkeypatch, capsys):
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "greenhouse", "acme")
    seen = []

    class Watching(Scripted):
        def fetch(self, ref, *, company=None):
            cli.main(["status"])                               # what a second terminal would see
            seen.append(capsys.readouterr().out)
            return super().fetch(ref, company=company)
    script = Watching(acme=[[mk("a1", "PM")]])
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: scout.run_scout(c, adapter_for=script))
    assert cli.main(["daily", "--recheck-days", "0", "--harvest-days", "0"]) == 0
    (during,) = seen
    line = next(l for l in during.splitlines() if l.startswith("Right now:"))
    assert "daily job, started " in line and "; now: scout, started " in line and "0 of 1 boards" in line
    cli.main(["status"])
    assert "Right now:      Idle" in capsys.readouterr().out


# --- jobs that did not finish -----------------------------------------------------

def test_a_job_whose_process_died_is_reported_until_the_next_one_starts(conn, capsys):
    companies.add_company(conn, "Acme", website="https://acme.example")
    insert_job(conn, "resolve", pid=dead_pid(), done=3, total=40, unit="companies")
    state = jobs.current(conn, NOW)
    assert state["running"] == [] and state["stopped"][0]["died"] is True
    assert state["line"] == ("Idle. A resolve that started 07:00 died without finishing "
                             "(stopped at 3 of 40 companies)")
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "  ! a resolve started 2026-03-10 07:00 UTC died without finishing" in out
    assert "died without finishing (stopped at 3 of 40 companies)" in out
    # The next resolve replaces it.
    resolve.resolve_companies(conn, resolver=resolve.Resolver(session=FakeWeb({})))
    assert jobs.current(conn)["line"] == "Idle"


def test_a_scout_that_died_is_reported_as_before_with_how_far_it_got(conn):
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "greenhouse", "acme")
    with transaction(conn):
        conn.execute("INSERT INTO runs (started_at, status, boards_total) VALUES ('2026-03-10T07:00:00Z', 'running', 9)")
    insert_job(conn, "scout", pid=dead_pid(), done=4, total=9, unit="boards")
    status = ops.collect_status(conn, now=NOW)
    assert status["attention"] == ["the last run never finished (the process died)"]      # said once
    assert status["now"]["line"] == ("Idle. A scout that started 07:00 died without finishing "
                                     "(stopped at 4 of 9 boards)")


def test_a_job_that_stops_on_an_error_is_reported_and_a_cancelled_one_is_not(conn):
    with pytest.raises(RuntimeError):
        with jobs.track(conn, "harvest", total=4, unit="boards") as job:
            job.advance(1)
            raise RuntimeError("disk full")
    state = jobs.current(conn)
    assert state["running"] == [] and state["stopped"][0]["error"] == "RuntimeError: disk full"
    assert "stopped on an error at 1 of 4 boards: RuntimeError: disk full" in state["line"]
    assert any("a harvest started" in a and "stopped on an error" in a for a in ops.collect_status(conn)["attention"])
    with pytest.raises(KeyboardInterrupt):
        with jobs.track(conn, "harvest"):
            raise KeyboardInterrupt
    assert jobs.current(conn)["line"] == "Idle"                # the earlier one was replaced, this one removed


# --- digest -----------------------------------------------------------------------

RANKING = SMALL_CONFIG.replace("size = 3", "size = 20")


@pytest.fixture
def three_runs(conn, isolated_db_path):
    """Three scouts. Between the second and the third a company is added and
    mapped; in the third a role closes, roles open, and a board starts failing."""
    (isolated_db_path.parent / "ranking.toml").write_text(RANKING)
    cfg = ranking.parse_config(RANKING, "test")
    for name, tier in (("Alpha", 1), ("Beta", 2)):
        companies.add_company(conn, name, tier=tier)
        companies.add_board(conn, name, "greenhouse", name.lower())
    with transaction(conn):                                    # both were there from the start
        conn.execute("UPDATE companies SET created_at = '2025-12-01T00:00:00Z'")
        conn.execute("UPDATE boards SET mapped_at = '2025-12-01T00:00:00Z'")
    a1, a2 = mk("a1", "Senior Product Manager"), mk("a2", "Product Manager")
    a3 = mk("a3", "Group Product Manager")
    b1, b2 = mk("b1", "Product Manager"), mk("b2", "Staff Product Manager")
    script = Scripted(
        alpha=[[a1, a2], [a1], [a1, a3]],                      # a2 is missed twice: closed in run 3
        beta=[[b1], [b1, b2], SourceError("HTTP 500")],
        gamma=[[mk("g1", "Product Manager"), mk("g2", "Software Engineer")]])
    clock = Clock()
    for run in (1, 2, 3):
        if run == 3:
            companies.add_company(conn, "Gamma", tier=2)
            companies.add_board(conn, "Gamma", "greenhouse", "gamma")
            with transaction(conn):
                conn.execute("UPDATE companies SET created_at = '2026-01-02T09:00:00Z' WHERE name = 'Gamma'")
                conn.execute("UPDATE boards SET mapped_at = '2026-01-02T09:00:00Z' WHERE ref = 'gamma'")
        clock.start()
        summary = scout.run_scout(conn, adapter_for=script, clock=clock)
        ranking.build_shortlist(conn, cfg, run_id=summary.run_id,
                                now=datetime(2026, 1, run, 12, tzinfo=timezone.utc))
    return conn


AT = datetime(2026, 1, 3, 13, tzinfo=timezone.utc)


def test_digest_covers_what_changed_since_the_previous_completed_scout(three_runs):
    d = digest.build_digest(three_runs, now=AT)
    assert (d["compared_with_run"], d["latest_run"]) == ("2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z")
    roles = d["roles"]
    assert (roles["opened"], roles["closed"]) == (1, 1)        # a3 opened, a2 closed; Gamma's are a baseline
    assert roles["top_opened"] == [{"company": "Alpha", "count": 1}]
    assert roles["top_closed"] == [{"company": "Alpha", "count": 1}]
    assert d["first_fetched"] == [{"company": "Gamma", "open_roles": 2}]
    new = [(e["company"], e["title"]) for e in d["shortlist"]["new"]]
    assert (d["shortlist"]["compared_with"], d["shortlist"]["new_count"]) == ("2026-01-02", 2)
    assert sorted(new) == [("Alpha", "Group Product Manager"), ("Gamma", "Product Manager")]
    assert all(isinstance(e["rank"], int) for e in d["shortlist"]["new"])
    assert d["companies"] == {
        "added": ["Gamma"], "mapped": ["Gamma"],
        "newly_failing": [{"company": "Beta", "board": "greenhouse/beta", "error": "HTTP 500"}]}
    assert d["coverage"]["now"] == {"companies": 3, "mapped": 3, "fetched_ok": 2, "open_roles": 6}
    assert d["coverage"]["then"] == {"companies": 2, "mapped": 2, "fetched_ok": 2, "open_roles": 4}


def test_digest_text_is_short_enough_for_a_phone(three_runs, capsys):
    assert cli.main(["digest"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].startswith("rolescout digest, ") and lines[0].endswith("since the scout of Fri 2 Jan 00:00")
    assert "Roles: 1 opened, 1 closed; open now 6 (was 4)" in lines
    assert "  Opened most: Alpha 1" in lines and "  Closed most: Alpha 1" in lines
    assert "Fetched for the first time (1): Gamma (2)" in lines
    assert "Shortlist: 2 new of 5 (against the list of 2026-01-02)" in lines
    assert any(l.startswith("  #") and l.endswith("Alpha: Group Product Manager") for l in lines)
    assert "Companies: 1 added, 1 mapped, 1 newly failing" in lines
    assert "  Added: Gamma" in lines and "  Mapped: Gamma" in lines
    assert "  Failing: Beta (greenhouse/beta): HTTP 500" in lines
    assert "Coverage: 3 (was 2) of 3 (was 2) companies mapped; 2 fetched without error" in lines
    assert len(lines) <= 16 and max(len(l) for l in lines) <= 80


def test_digest_since_a_date_compares_with_the_scout_before_that_day(three_runs, capsys):
    d = digest.build_digest(three_runs, since="2026-01-02", now=AT)
    assert d["compared_with_run"] == "2026-01-01T00:00:00Z"
    assert (d["roles"]["opened"], d["roles"]["closed"]) == (2, 1)          # b2 in run 2, a3 in run 3
    assert [t["company"] for t in d["roles"]["top_opened"]] == ["Alpha", "Beta"]
    assert d["shortlist"]["compared_with"] == "2026-01-01" and d["shortlist"]["new_count"] == 3
    assert cli.main(["digest", "--since", "2026-01-02", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["compared_with_run"] == "2026-01-01T00:00:00Z"
    assert cli.main(["digest", "--since", "yesterday"]) == 1
    assert "--since must be a date like 2026-01-31" in capsys.readouterr().err
    # A date after the last scout: nothing has completed since.
    late = digest.build_digest(three_runs, since="2026-02-01", now=AT)
    assert late["note"] == "No scout has completed since then." and late["roles"]["opened"] == 0


def test_digest_before_and_after_the_first_scout(conn, capsys):
    assert cli.main(["digest"]) == 0
    assert "No scout has completed yet." in capsys.readouterr().out
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "greenhouse", "acme")
    scout.run_scout(conn, adapter_for=Scripted(acme=[[mk("a1", "PM")]]))
    assert cli.main(["digest"]) == 0
    out = capsys.readouterr().out
    assert "nothing earlier to compare with" in out and "This was the first completed scout" in out
    assert "Fetched for the first time (1): Acme (1)" in out and "Roles: 0 opened, 0 closed; open now 1" in out


def test_the_daily_job_ends_its_log_with_the_digest(three_runs, monkeypatch, capsys):
    script = Scripted(alpha=[[mk("a1", "Senior Product Manager")]], beta=[[mk("b1", "Product Manager")]],
                      gamma=[[mk("g1", "Product Manager")]])
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: scout.run_scout(c, adapter_for=script))
    assert cli.main(["daily", "--recheck-days", "0", "--harvest-days", "0"]) == 0
    out = capsys.readouterr().out
    assert out.index("Run 4 completed") < out.index("rolescout digest, ") < out.index("=== finished")
    assert "since the scout of Sat 3 Jan 00:00" in out


# --- over MCP -----------------------------------------------------------------------

def test_mcp_health_says_what_is_running_and_digest_returns_the_digest(three_runs, isolated_db_path):
    pytest.importorskip("mcp")
    from rolescout import mcp_server
    from tests.test_mcp import Session
    session = Session(mcp_server.build_server(isolated_db_path, read_only=True))
    health = session.call("health")
    assert health["right_now"] == "Idle" and health["jobs_running"] == []
    with jobs.track(three_runs, "scout", total=725, unit="boards") as job:
        job.advance(412)
        health = session.call("health")
    assert health["right_now"].startswith("scout, started ") and "412 of 725 boards" in health["right_now"]
    (running,) = health["jobs_running"]
    assert (running["kind"], running["done"], running["total"], running["unit"]) == ("scout", 412, 725, "boards")
    insert_job(three_runs, "harvest", pid=dead_pid(), done=1, total=4, unit="boards")
    assert "A harvest that started" in session.call("health")["right_now"]

    result = session.call("digest")
    assert result["text"].startswith("rolescout digest, ") and "Roles: 1 opened, 1 closed" in result["text"]
    assert result["companies"]["added"] == ["Gamma"] and result["shortlist"]["new_count"] == 2
    assert session.call("digest", since="2026-01-02")["roles"]["opened"] == 2
    assert "--since must be a date" in session.error("digest", since="last week")
