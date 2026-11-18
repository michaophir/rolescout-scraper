"""A read counts as complete when it saw at least 99% of the roles the site
reports. Seen and reported are stored per fetch, and boards read below that
are shown in status."""

import re

import pytest

from rolescout import cli, companies, db, scout
from rolescout.adapters.base import COMPLETE_SHARE, FetchStats
from rolescout.adapters.eightfold import EightfoldAdapter
from rolescout.adapters.rippling import RipplingAdapter
from rolescout.ops import collect_status, partial_reads
from tests.conftest import FakeWeb, FakeWebResponse
from tests.test_job_systems import RIPPLING_LIST
from tests.test_workday import Scripted, board_of, posting, quiet as quiet_workday


@pytest.mark.parametrize("seen, reported, complete", [
    (100, 100, True), (99, 100, True), (98, 100, False), (2342, 2353, True), (2321, 2341, True),
    (2317, 2341, False), (0, 0, True), (0, 5, False), (12, 10, True),
])
def test_the_threshold_is_99_percent_of_what_the_site_reports(seen, reported, complete):
    assert COMPLETE_SHARE == 0.99
    stats = FetchStats()
    stats.count(seen, reported)
    assert (stats.seen, stats.reported, stats.complete) == (seen, reported, complete)
    assert bool(stats.notes) is not complete
    if not complete:
        assert stats.notes[0].startswith(f"saw {seen} of the {reported} roles listed (") and "under 99%" in stats.notes[0]


def test_a_read_cut_short_for_another_reason_stays_partial_whatever_the_share():
    stats = FetchStats(complete=False)
    stats.count(100, 100)
    assert not stats.complete


# --- eightfold: a list of any length, read in overlapping pages ---------------------

class Listing:
    """A site listing ``count`` roles of which only ``returned`` ever come back."""

    def __init__(self, returned, count):
        self.ids, self.count, self.urls = list(returned), count, []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url.endswith("/robots.txt"):
            return FakeWebResponse(url, None, 404)
        start = int(re.search(r"[?&]start=(\d+)", url).group(1))
        positions = [{"id": n, "name": f"Role {n}", "locations": ["Here"], "t_create": 1794528000}
                     for n in self.ids[start:start + 10]]
        return FakeWebResponse(url, {"count": self.count, "positions": positions}, 200)


def eightfold_scout(conn, returned, count):
    adapter = EightfoldAdapter(Listing(returned, count), min_interval=0, sleep=lambda s: None)
    (board,) = scout.run_scout(conn, adapter_for=lambda source: adapter).boards
    return board


@pytest.fixture
def acme(conn):
    companies.add_company(conn, "Acme")
    companies.add_board(conn, "Acme", "eightfold", "acme.eightfold.ai/acme.com")
    return conn


def open_roles(conn):
    return conn.execute("SELECT COUNT(*) FROM roles WHERE closed_at IS NULL").fetchone()[0]


def test_a_read_within_one_percent_is_complete_and_closes_on_the_second_miss(acme):
    board = eightfold_scout(acme, range(300), 300)
    assert (board.roles_fetched, board.roles_seen, board.roles_reported, board.complete) == (300, 300, 300, True)

    # The site lists 300 and returns 298 (99.3%): complete. Roles 0 and 1 are gone.
    for expected_open in (300, 298):                     # first miss, then second
        board = eightfold_scout(acme, range(2, 300), 300)
        assert (board.roles_seen, board.roles_reported, board.complete, board.note) == (298, 300, True, "")
        assert open_roles(acme) == expected_open
    rows = acme.execute("SELECT roles_seen, roles_reported, complete FROM run_boards ORDER BY run_id").fetchall()
    assert [tuple(r) for r in rows] == [(300, 300, 1), (298, 300, 1), (298, 300, 1)]
    assert partial_reads(acme) == []


def test_a_read_below_99_percent_closes_nothing_and_is_shown_until_a_fuller_read(acme, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    eightfold_scout(acme, range(300), 300)
    for _ in range(3):                                   # however often: 96% is never a miss
        board = eightfold_scout(acme, range(12, 300), 300)
        assert (board.status, board.roles_seen, board.roles_reported, board.complete) == ("ok", 288, 300, False)
        assert board.note == "saw 288 of the 300 roles listed (96.0%, under 99%)"
        assert open_roles(acme) == 300
    assert tuple(acme.execute("SELECT roles_seen, roles_reported, complete FROM run_boards "
                              "ORDER BY run_id DESC").fetchone()) == (288, 300, 0)

    (shown,) = partial_reads(acme)
    assert (shown["company"], shown["source"], shown["ref"]) == ("Acme", "eightfold", "acme.eightfold.ai/acme.com")
    assert (shown["roles_seen"], shown["roles_reported"], shown["percent"]) == (288, 300, 96.0)
    assert collect_status(acme)["partial_reads"] == [shown]
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "Partial reads:  1 board last read below 99% of the roles listed" in out
    assert "Read below 99% of the roles listed:" in out
    row = next(line for line in out.splitlines() if line.startswith("Acme "))
    assert row.split()[:5] == ["Acme", "eightfold/acme.eightfold.ai/acme.com", "288", "300", "96.0%"]

    # The progress line of a scout says it too.
    cli._print_progress(board)
    assert "288 roles (288 of 300 listed), partial" in capsys.readouterr().out
    # A fuller read clears it, and a failed fetch in between does not hide it.
    eightfold_scout(acme, range(12, 300), 300)
    assert len(partial_reads(acme)) == 1
    eightfold_scout(acme, range(300), 300)
    assert partial_reads(acme) == []
    cli.main(["status"])
    assert "Partial reads:  none" in capsys.readouterr().out


def test_health_over_mcp_lists_partial_reads(acme, isolated_db_path):
    pytest.importorskip("mcp")
    from rolescout import mcp_server
    from tests.test_mcp import Session
    eightfold_scout(acme, range(300), 300)
    eightfold_scout(acme, range(30, 300), 300)
    health = Session(mcp_server.build_server(isolated_db_path)).call("health")
    assert health["partial_reads"] == [
        "Acme (eightfold/acme.eightfold.ai/acme.com): saw 270 of 300 roles listed (90.0%)"]


# --- the other sources that report a total ------------------------------------------

def test_rippling_compares_the_entries_read_with_the_number_listed(load_fixture):
    listing = load_fixture("rippling_role_listed_once_per_location.json")
    adapter = RipplingAdapter(FakeWeb({RIPPLING_LIST.format("acme", 0): listing}))
    adapter.fetch("acme")
    stats = adapter.last_stats
    assert (stats.seen, stats.reported, stats.complete) == (7, 7, True)

    short = RipplingAdapter(FakeWeb({RIPPLING_LIST.format("acme", 0): {**listing, "totalItems": 9}}))
    short.fetch("acme")
    stats = short.last_stats
    assert (stats.seen, stats.reported, stats.complete) == (7, 9, False)
    assert stats.notes == ["saw 7 of the 9 roles listed (77.8%, under 99%)"]


def test_workday_compares_the_postings_read_with_the_total_reported():
    adapter = quiet_workday(board_of(45))
    assert len(adapter.fetch("acme/wd5/Careers")) == 45
    assert (adapter.last_stats.seen, adapter.last_stats.reported, adapter.last_stats.complete) == (45, 45, True)

    def answer(listed, returned):
        def reply(method, url, body):
            if url.endswith("/robots.txt"):
                return {}
            offset = body["offset"]
            page = [posting(n) for n in range(offset, min(offset + 20, returned))]
            return {"total": listed if offset == 0 else 0, "jobPostings": page}
        return Scripted(reply)

    close = quiet_workday(answer(200, 199))              # 99.5%
    assert len(close.fetch("acme/wd5/Careers")) == 199
    assert (close.last_stats.seen, close.last_stats.reported, close.last_stats.complete) == (199, 200, True)
    short = quiet_workday(answer(200, 150))
    assert len(short.fetch("acme/wd5/Careers")) == 150
    assert (short.last_stats.seen, short.last_stats.reported, short.last_stats.complete) == (150, 200, False)
    assert "saw 150 of the 200 roles listed (75.0%, under 99%)" in short.last_stats.notes


def test_sources_that_report_no_total_record_none(conn, load_fixture):
    from rolescout.adapters.google import FEED_URL, GoogleAdapter
    from tests.test_eightfold_and_google import feed
    companies.add_company(conn, "Google")
    companies.add_board(conn, "Google", "google", "careers")
    (board,) = scout.run_scout(conn, adapter_for=lambda source: GoogleAdapter(FakeWeb({FEED_URL: feed()}))).boards
    assert (board.roles_seen, board.roles_reported, board.complete) == (None, None, True)
    assert tuple(conn.execute("SELECT roles_seen, roles_reported, complete FROM run_boards").fetchone()) == (None, None, 1)
    assert partial_reads(conn) == []


# --- existing databases ---------------------------------------------------------------

def test_an_older_database_gains_the_columns_and_its_history_counts_as_complete(tmp_path, monkeypatch):
    latest = db.SCHEMA_VERSION
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", latest - 1)
    old = db.connect(path)
    old.execute("INSERT INTO companies (name, website, created_at) VALUES ('Acme', '', 'x')")
    old.execute("INSERT INTO runs (started_at, status) VALUES ('2026-11-01T00:00:00Z', 'completed')")
    old.execute("INSERT INTO run_boards (run_id, company_id, source, ref, status, roles_fetched) "
                "VALUES (1, 1, 'lever', 'acme', 'ok', 4)")
    old.commit()
    old.close()
    monkeypatch.setattr(db, "SCHEMA_VERSION", latest)

    conn = db.connect(path)
    assert db.schema_version(conn) == latest
    assert tuple(conn.execute("SELECT roles_fetched, roles_seen, roles_reported, complete FROM run_boards").fetchone()) == (
        4, None, None, 1)
    conn.close()
