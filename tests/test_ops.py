"""Lock, backup, log rotation, status, the daily job and the launchd schedule."""

import json
import plistlib
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timezone

import pytest

from rolescout import cli, companies, db, ops, scout
from rolescout import schedule as launchd
from rolescout.adapters import SourceError
from rolescout.db import transaction
from rolescout.scout import ScoutAlreadyRunning, run_scout, scout_lock
from tests.test_scout import Clock, Scripted, role


@pytest.fixture
def acme(conn):
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "greenhouse", "acme")
    return conn


def scripted(monkeypatch, script):
    """Make the CLI's scout use scripted fetches instead of the network."""
    monkeypatch.setattr(cli, "run_scout", lambda conn, **kw: scout.run_scout(
        conn, adapter_for=script, on_board=kw.get("on_board")))


# --- single-run lock ---------------------------------------------------------

def test_second_scout_in_the_same_process_is_refused(acme, isolated_db_path):
    other = db.connect(isolated_db_path)
    with scout_lock(other):
        with pytest.raises(ScoutAlreadyRunning, match=r"already running .*\(pid \d+\)"):
            run_scout(acme, adapter_for=Scripted(acme=[[role("A")]]))
        assert scout.scout_is_running(acme)
    assert acme.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0  # nothing was started
    # Released: the next scout runs.
    assert not scout.scout_is_running(acme)
    assert run_scout(acme, adapter_for=Scripted(acme=[[role("A")]])).ok == 1


def test_lock_is_held_by_another_process_and_dies_with_it(acme, isolated_db_path, capsys, monkeypatch):
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys
            from rolescout import db
            from rolescout.scout import scout_lock
            conn = db.connect({str(isolated_db_path)!r})
            with scout_lock(conn):
                print("locked", flush=True)
                sys.stdin.read()
        """)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        scripted(monkeypatch, Scripted(acme=[[role("A")]]))
        # The CLI exits cleanly with a message; it is not an error.
        assert cli.main(["scout"]) == 0
        out = capsys.readouterr().out
        assert f"Not starting: another scout is already running on this database (pid {holder.pid})." in out
        assert acme.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        holder.kill()  # a crash, not a clean exit
        holder.wait()
    assert cli.main(["scout"]) == 0  # no stale lock left behind
    assert "Run 1 completed" in capsys.readouterr().out


# --- backup ------------------------------------------------------------------

def at(day, hour=0):
    return datetime(2026, 1, day, hour, tzinfo=timezone.utc)


def test_backup_while_a_scout_is_writing_is_a_consistent_snapshot(acme, isolated_db_path, tmp_path):
    run_scout(acme, adapter_for=Scripted(acme=[[role("A"), role("B")]]))
    writer = db.connect(isolated_db_path)
    writer.execute("BEGIN IMMEDIATE")  # a scout mid-company: uncommitted changes, write lock held
    writer.execute("UPDATE roles SET title = 'HALF WRITTEN'")
    writer.execute("DELETE FROM role_descriptions")

    target, removed = ops.backup_database(acme, tmp_path / "backups", now=at(1))
    assert target.name == "rolescout-20260101-000000.db" and removed == []
    writer.execute("COMMIT")

    copy = sqlite3.connect(target)
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # The copy holds the last committed state, none of the in-flight transaction.
    assert [r[0] for r in copy.execute("SELECT title FROM roles")] == ["Engineer", "Engineer"]
    assert copy.execute("SELECT COUNT(*) FROM role_descriptions").fetchone()[0] == 2
    assert copy.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    copy.close()
    # One self-contained file, nothing left over.
    assert sorted(p.name for p in (tmp_path / "backups").iterdir()) == [target.name]
    # The live database got the writer's changes; the backup did not disturb it.
    assert acme.execute("SELECT DISTINCT title FROM roles").fetchone()[0] == "HALF WRITTEN"


def test_backup_keeps_the_last_n(acme, tmp_path):
    folder = tmp_path / "b"
    for day in range(1, 6):
        target, removed = ops.backup_database(acme, folder, keep=3, now=at(day))
    assert [p.name for p in removed] == ["rolescout-20260102-000000.db"]
    assert [p.name for p in ops.list_backups(folder)] == [
        "rolescout-20260103-000000.db", "rolescout-20260104-000000.db", "rolescout-20260105-000000.db"]
    (folder / "notes.txt").write_text("not a backup")  # other files are never touched
    ops.backup_database(acme, folder, keep=1, now=at(6))
    assert sorted(p.name for p in folder.iterdir()) == ["notes.txt", "rolescout-20260106-000000.db"]
    with pytest.raises(ops.BackupError):
        ops.backup_database(acme, folder, keep=0)


def test_backup_command(acme, tmp_path, capsys):
    assert cli.main(["backup", str(tmp_path / "b"), "--keep", "2"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Backup written: {tmp_path / 'b'}/rolescout-") and ("KB)" in out or "MB)" in out)
    assert len(ops.list_backups(tmp_path / "b")) == 1


# --- log rotation ------------------------------------------------------------

def test_log_rotation_keeps_three_old_files(tmp_path):
    log = tmp_path / "scout.log"
    assert ops.rotate_log(log) is False  # nothing there yet
    for generation in range(1, 6):
        log.write_text(f"generation {generation} " + "x" * 200)
        assert ops.rotate_log(log, max_bytes=100) is True
        assert not log.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["scout.log.1", "scout.log.2", "scout.log.3"]
    assert (tmp_path / "scout.log.1").read_text().startswith("generation 5")
    assert (tmp_path / "scout.log.3").read_text().startswith("generation 3")
    log.write_text("small")
    assert ops.rotate_log(log, max_bytes=100) is False


# --- status ------------------------------------------------------------------

def test_status_before_any_run(conn, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("NEEDS ATTENTION\n  ! no scout has run yet\n")
    assert "Last run:       never" in out


def test_status_reports_health_from_the_database_alone(conn, monkeypatch, capsys, tmp_path, isolated_db_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("Acme", "Bolt", "Cobalt"):
        companies.add_company(conn, name)
        companies.add_board(conn, name, "greenhouse", name.lower())
    companies.add_board(conn, "Cobalt", "lever", "cobalt-eu")
    down = SourceError("greenhouse: HTTP 500 for 'bolt'")
    script = Scripted(**{
        "acme": [[role("A1"), role("A2")]] * 4,
        "bolt": [[role("B1")], down, down, down],
        "cobalt": [[role("C1")]] * 4,
        "cobalt-eu": [[role("E1", source="lever")]] * 3 + [SourceError("lever: timeout")],
    })
    clock = Clock()
    for _ in range(3):
        clock.start()
        run_scout(conn, adapter_for=script, clock=clock)

    now = datetime(2026, 1, 3, 6, 0, tzinfo=timezone.utc)
    status = ops.collect_status(conn, schedule=launchd.read_schedule(), now=now)
    # Two failures in a row is not yet "more than two".
    assert status["failing_boards"] == [] and status["healthy"] is True
    assert status["last_run"]["hours_ago"] == 6.0

    clock.start()
    run_scout(conn, adapter_for=script, clock=clock)
    status = ops.collect_status(conn, schedule=launchd.read_schedule(),
                                now=datetime(2026, 1, 4, 1, 30, tzinfo=timezone.utc))
    last = status["last_run"]
    assert (last["status"], last["started_at"], last["hours_ago"]) == ("completed", "2026-01-04T00:00:00Z", 1.5)
    assert (last["companies_ok"], last["companies_failed"]) == (1, 2)   # Cobalt counts as failed: one of its boards did
    assert (last["boards_ok"], last["boards_failed"]) == (2, 2)
    assert [(b["company"], b["ref"], b["failed_runs_in_a_row"]) for b in status["failing_boards"]] == [
        ("Bolt", "bolt", 3)]                                            # cobalt-eu failed once: not listed
    assert status["open_roles"] == 5
    assert (status["companies_active"], status["companies_mapped"]) == (3, 3)
    assert status["database"] == str(isolated_db_path) and status["database_bytes"] > 0
    assert status["scout_running"] is False
    assert status["healthy"] is False
    assert status["attention"] == [
        "Bolt (greenhouse/bolt) has failed 3 runs in a row: greenhouse: HTTP 500 for 'bolt'"]

    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert out.startswith("NEEDS ATTENTION\n  ! Bolt (greenhouse/bolt) has failed 3 runs in a row")
    assert "Last run:       2026-01-04T00:00:00Z" in out and ", completed" in out
    assert "Companies:      1 ok, 2 failed (2 boards ok, 2 failed)" in out
    assert "Failing boards: 1 failing more than two runs in a row" in out
    assert "Open roles:     5 across 3 mapped companies (3 active)" in out
    assert f"Database:       {isolated_db_path} (" in out
    assert "Next run:       not scheduled" in out
    assert "Failed in the last run:" in out and "lever/cobalt-eu" in out and "lever: timeout" in out

    assert cli.main(["status", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["failing_boards"][0]["company"] == "Bolt"


def test_status_flags_stale_interrupted_and_offline_runs(acme, monkeypatch):
    schedule = {"supported": True, "installed": True, "loaded": True, "time": "07:30"}
    clock = Clock()
    clock.start()
    run_scout(acme, adapter_for=Scripted(acme=[[role("A")]]), clock=clock)
    fresh = ops.collect_status(acme, schedule=schedule, now=datetime(2026, 1, 2, 1, tzinfo=timezone.utc))
    assert fresh["healthy"] is True
    stale = ops.collect_status(acme, schedule=schedule, now=datetime(2026, 1, 2, 3, tzinfo=timezone.utc))
    assert stale["attention"] == ["no run for 27 hours although a daily run is scheduled"]
    # Without a schedule, an old run is not a fault.
    assert ops.collect_status(acme, schedule={"supported": True, "installed": False, "loaded": False},
                              now=datetime(2026, 2, 1, tzinfo=timezone.utc))["healthy"] is True

    clock.start()
    run_scout(acme, adapter_for=Scripted(acme=[SourceError("no network")]), clock=clock)
    offline = ops.collect_status(acme, now=datetime(2026, 1, 2, 1, tzinfo=timezone.utc))
    assert offline["attention"] == ["the last run could not fetch any board (network down?)"]

    with transaction(acme):
        acme.execute("INSERT INTO runs (started_at, status) VALUES ('2026-01-03T00:00:00Z', 'running')")
    dead = ops.collect_status(acme, now=datetime(2026, 1, 3, 1, tzinfo=timezone.utc))
    assert dead["attention"] == ["the last run never finished (the process died)"]
    with scout_lock(db.connect(scout.database_file(acme))):
        running = ops.collect_status(acme, now=datetime(2026, 1, 3, 1, tzinfo=timezone.utc))
    assert running["scout_running"] is True and running["healthy"] is True

    unloaded = ops.collect_status(acme, schedule={"supported": True, "installed": True, "loaded": False},
                                  now=datetime(2026, 1, 3, 1, tzinfo=timezone.utc))
    assert any("launchd has not loaded it" in a for a in unloaded["attention"])


# --- the daily job -----------------------------------------------------------

def test_daily_scouts_backs_up_and_logs(acme, monkeypatch, tmp_path, capsys):
    scripted(monkeypatch, Scripted(acme=[[role("A"), role("B")], [role("A"), role("B")]]))
    log, backups = tmp_path / "logs" / "scout.log", tmp_path / "backups"
    ops.backup_database(acme, backups, now=at(1))  # an older backup, to be pruned
    argv = ["daily", "--log", str(log), "--backup-dir", str(backups), "--keep", "1"]
    stdout = sys.stdout
    try:
        assert cli.main(argv) == 0
        assert cli.main(argv) == 0
    finally:
        sys.stdout, sys.stderr = stdout, sys.__stderr__
    assert capsys.readouterr().out == ""  # everything went to the log
    text = log.read_text()
    assert text.count("=== rolescout daily, ") == 2 and text.count(", ok ===") == 2
    assert "Run 1 completed" in text and "Run 2 completed" in text
    assert "Backup written: " in text and "removed 1 older backup" in text
    assert len(ops.list_backups(backups)) == 1


def test_daily_retries_when_every_board_fails_then_reports_failure(acme, monkeypatch, capsys):
    waits = []
    monkeypatch.setattr(cli.time, "sleep", waits.append)
    scripted(monkeypatch, Scripted(acme=[SourceError("offline"), [role("A")]]))
    assert cli.main(["daily", "--retry-wait", "5"]) == 0
    out = capsys.readouterr().out
    assert "Every board failed (attempt 1 of 3); retrying in 5 seconds." in out
    assert waits == [5.0] and "Run 2 completed" in out

    scripted(monkeypatch, Scripted(acme=[SourceError("offline")] * 3))
    assert cli.main(["daily", "--retry-wait", "5"]) == 1
    out = capsys.readouterr().out
    assert "Every board failed on all 3 attempts." in out and "FAILED ===" in out


def test_daily_skips_cleanly_when_a_scout_is_running(acme, monkeypatch, capsys, tmp_path, isolated_db_path):
    scripted(monkeypatch, Scripted(acme=[[role("A")]]))
    with scout_lock(db.connect(isolated_db_path)):
        assert cli.main(["daily", "--backup-dir", str(tmp_path / "b")]) == 0
    assert "Skipping this run." in capsys.readouterr().out
    assert ops.list_backups(tmp_path / "b") == []


# --- the launchd schedule ----------------------------------------------------

@pytest.fixture
def fake_launchd(monkeypatch, tmp_path):
    """A Mac with a home folder in tmp and a launchctl that only records calls."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(launchd.sys, "platform", "darwin")
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501, raising=False)
    state = {"calls": [], "loaded": set(), "refuse": set()}

    def launchctl(*args):
        state["calls"].append(args)
        verb, target = args[0], args[1]
        code = 0
        if verb == "bootstrap":
            if target in state["refuse"]:
                return subprocess.CompletedProcess(args, 5, "", "Bootstrap failed: 5: Input/output error")
            state["loaded"].add(f"{target}/{launchd.LABEL}")
        elif verb == "bootout":
            code = 0 if target in state["loaded"] else 3
            state["loaded"].discard(target)
        elif verb == "print":
            code = 0 if target in state["loaded"] else 113
        return subprocess.CompletedProcess(args, code, "", "")
    monkeypatch.setattr(launchd, "_launchctl", launchctl)
    return state


def test_schedule_install_status_and_remove(conn, fake_launchd, tmp_path, capsys, isolated_db_path):
    assert cli.main(["schedule", "install", "--at", "07:30", "--keep", "5"]) == 0
    out = capsys.readouterr().out
    plist_file = tmp_path / "Library" / "LaunchAgents" / "com.getrolescout.scout.plist"
    assert "Scheduled: rolescout scouts daily at 07:30 local time, then backs up." in out
    assert f"Schedule:  {plist_file} (launchd domain gui/501)" in out

    plist = plistlib.loads(plist_file.read_bytes())
    assert plist["Label"] == "com.getrolescout.scout"
    assert plist["StartCalendarInterval"] == {"Hour": 7, "Minute": 30}  # fires on wake if missed
    assert plist["RunAtLoad"] is False
    log_dir = tmp_path / "Library" / "Logs" / "rolescout"
    assert plist["ProgramArguments"] == [
        sys.executable, "-m", "rolescout", "daily", "--db", str(isolated_db_path.resolve()),
        "--backup-dir", str((tmp_path / ".rolescout" / "backups").resolve()), "--keep", "5",
        "--log", str(log_dir.resolve() / "scout.log")]
    assert ("bootstrap", "gui/501", str(plist_file)) in fake_launchd["calls"]

    info = launchd.read_schedule(now=datetime(2026, 1, 1, 9, 0))
    assert (info["loaded"], info["time"], info["next_run"]) == (True, "07:30", "2026-01-02 07:30")
    assert launchd.read_schedule(now=datetime(2026, 1, 1, 6, 0))["next_run"] == "2026-01-01 07:30"
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "local time (daily at 07:30)" in out and f"Log:            {log_dir.resolve() / 'scout.log'}" in out

    # Installing again replaces the schedule rather than adding a second one.
    assert cli.main(["schedule", "install", "--at", "18:05"]) == 0
    capsys.readouterr()
    assert plistlib.loads(plist_file.read_bytes())["StartCalendarInterval"] == {"Hour": 18, "Minute": 5}
    assert fake_launchd["loaded"] == {"gui/501/com.getrolescout.scout"}

    assert cli.main(["schedule", "remove"]) == 0
    assert "Schedule removed" in capsys.readouterr().out
    assert not plist_file.exists() and fake_launchd["loaded"] == set()
    assert launchd.read_schedule()["installed"] is False
    assert cli.main(["schedule", "remove"]) == 0
    assert capsys.readouterr().out == "No schedule was installed.\n"


def test_schedule_falls_back_to_the_user_domain_without_a_desktop_session(conn, fake_launchd, capsys):
    fake_launchd["refuse"].add("gui/501")
    assert cli.main(["schedule", "install", "--at", "07:30"]) == 0
    out = capsys.readouterr().out
    assert "(launchd domain user/501)" in out and "loaded without a desktop session" in out

    fake_launchd["refuse"].add("user/501")
    assert cli.main(["schedule", "install", "--at", "07:30"]) == 1
    assert "launchd would not load it" in capsys.readouterr().err


def test_schedule_rejects_bad_times(conn, fake_launchd, capsys):
    for bad in ("7.30", "25:00", "07:61", "soon"):
        assert cli.main(["schedule", "install", "--at", bad]) == 1
        assert "time must be HH:MM" in capsys.readouterr().err


def test_schedule_on_other_systems_explains_and_offers_cron(conn, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(launchd.sys, "platform", "linux")
    assert cli.main(["schedule", "install", "--at", "07:30"]) == 1
    err = capsys.readouterr().err
    assert "scheduling is built for macOS (launchd) only" in err
    assert "30 7 * * * " in err and "-m rolescout daily --db" in err
    assert not (tmp_path / "Library").exists()
    assert cli.main(["schedule", "remove"]) == 1
    capsys.readouterr()
    cli.main(["status"])
    assert "not scheduled (rolescout schedules itself on macOS only)" in capsys.readouterr().out
