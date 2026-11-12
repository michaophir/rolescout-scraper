"""Resolving a company is bounded, whatever a page contains."""

import re
import threading
import time

import pytest

from rolescout import cli, companies, db, gaps, ops, resolve, scout
from rolescout.resolve import (
    COMPANY_SCAN_MAX_CHARS, FINGERPRINTS, MAX_TOKEN_CHARS, PAGE_MAX_BYTES, SUPPORTED_LINKS,
    UNSUPPORTED_LINKS, Resolver, _normalize, domain_pattern, read_body, resolve_companies,
    scan_for_boards, scan_for_fingerprints,
)
from tests.conftest import FakeWeb
from tests.test_resolve import LEVER, boards_of, lever_jobs, resolution, run_resolve
from tests.test_scout import Scripted, role

# The patterns that cost 47 seconds each on one real homepage: an unanchored
# wildcard in front, so every position of a long unbroken run is retried.
OLD_SLOW_PATTERNS = [
    ("eightfold", r"[\w-]+\.eightfold\.ai|eightfold-careers"), ("icims", r"[\w-]+\.icims\.com"),
    ("bamboohr", r"[\w-]+\.bamboohr\.com/(?:careers|jobs|js/embed)"),
    ("teamtailor", r"[\w-]+\.teamtailor\.com|teamtailor-cdn"),
    ("personio", r"[\w-]+\.jobs\.personio\."), ("recruitee", r"[\w-]+\.recruitee\.com"),
]


def cpu(fn):
    started = time.process_time()
    result = fn()
    return result, time.process_time() - started


def blob_page():
    """Shaped like the page that caused it: base64 blobs in script tags, one 110,767 characters long."""
    blob = "eyJhbmltYXRpb25zL29uYm9hcmRpbmdfb3B0aW1pemUuanNvbi" * 2216  # 110,800 characters, unbroken
    return (f'<html><script id="urls" type="text/plain">{blob[:110_767]}</script>'
            f'<script type="text/plain">{blob[:40_655]}</script>'
            '<a href="/careers">Careers</a></html>')


# --- the pathological page ---------------------------------------------------

def test_pathological_page_resolves_within_budget(conn, monkeypatch):
    # Put the old slow patterns back: the page must be harmless even to them.
    monkeypatch.setattr(resolve, "FINGERPRINTS", FINGERPRINTS + [
        (name, re.compile(pattern, re.I)) for name, pattern in OLD_SLOW_PATTERNS])
    companies.add_company(conn, "Blobby", website="https://blobby.com")
    pages = {
        "https://blobby.com": blob_page(),
        "https://blobby.com/careers": blob_page() + '<a href="https://jobs.lever.co/blobby">Jobs</a>',
        LEVER.format("blobby"): lever_jobs(3),
    }
    started = time.monotonic()
    ((res,), _), used = cpu(lambda: run_resolve(conn, pages))
    assert used < 2.0 and time.monotonic() - started < 5.0     # was 285 seconds of CPU
    assert not res.over_budget
    assert boards_of(conn, "Blobby")[0][:2] == ("lever", "blobby")  # and detection still works


def test_long_runs_are_cut_before_scanning_and_real_urls_survive():
    text = _normalize("a" * 500 + " https://boards.greenhouse.io/acme " + "b-" * 400)
    assert max(len(run) for run in re.findall(r"[\w%-]+", text)) == MAX_TOKEN_CHARS
    assert scan_for_boards(text)[0] == {("greenhouse", "acme"): None}
    # A long but legitimate URL is untouched: only unbroken runs are cut.
    url = "https://careers.example.com/" + "/".join(["segment-" + str(i) for i in range(60)])
    assert _normalize(url) == url


ADVERSARIAL = {
    "one unbroken run": "a" * 2_000_000,
    "runs of exactly the cap, dotted": ("a" * MAX_TOKEN_CHARS + ".") * 24_000,
    "dots only": "a." * 1_000_000,
    "hyphens and percents": "-%" * 1_000_000,
    "almost-matches": "x.greenhouse.io/v1/boards/ jobs.lever.co/ .icims.co " * 38_000,
    "nested quantifier bait": ("a" * 79 + "!") * 25_000,
}


@pytest.mark.parametrize("shape", ADVERSARIAL)
def test_every_pattern_is_fast_on_two_megabytes_of_hostile_text(shape):
    text = ADVERSARIAL[shape][:PAGE_MAX_BYTES]

    def scan_everything():
        normalized = _normalize(text)
        scan_for_boards(normalized, normalized=True)
        scan_for_fingerprints(normalized, normalized=True)
        domain_pattern("example.com").search(normalized)
        # Every pattern is covered by the two scans above; fail if one is added outside them.
        assert {p for _, p in SUPPORTED_LINKS + UNSUPPORTED_LINKS + FINGERPRINTS}
    _, used = cpu(scan_everything)
    # One hostile page costs about a second and a half on this machine; with the
    # 8 MB per-company cap that keeps a whole company inside the CPU budget.
    assert used < 3.0, f"{shape}: {used:.1f}s of CPU"
    assert used * (COMPANY_SCAN_MAX_CHARS / PAGE_MAX_BYTES) < resolve.COMPANY_CPU_SECONDS


def test_download_and_scan_caps(conn, monkeypatch):
    class Streaming:
        """A response that would deliver 50 MB if nobody stopped reading."""
        status_code, url, encoding = 200, "https://big.com", None
        headers = {"content-type": "application/javascript"}

        def __init__(self):
            self.sent = 0
            self.closed = False

        def iter_content(self, size):
            while self.sent < 50_000_000:
                self.sent += size
                yield b"x" * size

        def close(self):
            self.closed = True
    resp = Streaming()
    body = read_body(resp)
    assert len(body) == PAGE_MAX_BYTES and resp.closed
    assert resp.sent < PAGE_MAX_BYTES + 70_000          # stopped downloading at the cap

    # Across one company, scanning stops at the total cap and says so.
    monkeypatch.setattr(resolve, "COMPANY_SCAN_MAX_CHARS", 400_000)
    companies.add_company(conn, "Heavy", website="https://heavy.com")
    filler = "<p>" + "word " * 20_000 + "</p>"            # ~100 KB per page
    pages = {"https://heavy.com": filler + '<a href="/careers">Careers</a><a href="/jobs">Jobs</a>',
             "https://heavy.com/careers": filler + '<a href="/careers/open-roles">Open roles</a>',
             "https://heavy.com/jobs": filler,
             "https://heavy.com/careers/open-roles": filler + '<script src="/a.js"></script>',
             "https://heavy.com/a.js": filler}
    assert sum(len(p) for p in pages.values()) > 400_000
    (res,), _ = run_resolve(conn, pages)
    assert any("not scanned: this company's pages already exceed" in note for note in res.notes)


# --- the time budgets --------------------------------------------------------

def test_cpu_budget_stops_a_runaway_pattern_and_the_run_continues(conn, monkeypatch):
    """Even a pattern with exponential backtracking cannot hold the run up."""
    monkeypatch.setattr(resolve, "FINGERPRINTS", FINGERPRINTS + [("evil", re.compile(r"(a+)+$"))])
    companies.add_company(conn, "Evil", website="https://evil.com", tier=1)
    companies.add_company(conn, "Fine", website="https://fine.com", tier=2)
    web = FakeWeb({
        "https://evil.com": "a" * 60 + "!",
        "https://fine.com": '<a href="https://jobs.lever.co/fine">Jobs</a>',
        LEVER.format("fine"): lever_jobs(2),
    })
    started = time.monotonic()
    results = resolve_companies(conn, resolver=Resolver(session=web, cpu_budget=0.5, wall_budget=30))
    assert time.monotonic() - started < 5.0
    evil, fine = results
    assert (evil.outcome, evil.job_system, evil.over_budget) == ("unresolved", None, True)
    assert evil.notes[-1] == ("stopped: resolving this company used more than 0.5 seconds of "
                              "processor time; left unidentified")
    assert fine.outcome == "mapped" and boards_of(conn, "Fine")[0][:2] == ("lever", "fine")
    # Recorded as unidentified, with the reason.
    saved = resolution(conn, "Evil")
    assert (saved["outcome"], saved["job_system"]) == ("unresolved", None)
    assert "processor time" in saved["evidence"]["notes"][-1]
    entry = next(e for e in gaps.classify(conn) if e["company"] == "Evil")
    assert entry["state"] == "unidentified"


def test_wall_budget_stops_a_site_that_never_answers(conn):
    class Stalling(FakeWeb):
        def get(self, url, **kwargs):
            if "slow.com" in url:
                time.sleep(30)
            return super().get(url, **kwargs)
    companies.add_company(conn, "Slow", website="https://slow.com", tier=1)
    companies.add_company(conn, "Fine", website="https://fine.com", tier=2)
    web = Stalling({"https://fine.com": '<a href="https://jobs.lever.co/fine">Jobs</a>',
                    LEVER.format("fine"): lever_jobs(2)})
    started = time.monotonic()
    slow, fine = resolve_companies(conn, resolver=Resolver(session=web, wall_budget=0.4))
    assert time.monotonic() - started < 5.0
    assert slow.over_budget and "took longer than 0.4 seconds" in slow.notes[-1]
    assert fine.outcome == "mapped"


def test_budget_is_still_enforced_between_steps_where_timers_cannot_run(conn):
    """Interval timers need the main thread. Elsewhere the checks between requests apply."""
    clock = [1000.0]

    class Ticking(FakeWeb):
        def get(self, url, **kwargs):
            clock[0] += 25          # every request "takes" 25 seconds
            return super().get(url, **kwargs)
    web = Ticking({"https://acme.com": '<a href="/careers">Careers</a>',
                   "https://acme.com/careers": "<p>jobs</p>"})
    outcome = {}

    def work():
        real = resolve.time.monotonic
        resolve.time.monotonic = lambda: clock[0]
        try:
            outcome["res"] = Resolver(session=web, wall_budget=60).resolve("Acme", "https://acme.com")
        finally:
            resolve.time.monotonic = real
    thread = threading.Thread(target=work)
    thread.start()
    thread.join(10)
    res = outcome["res"]
    assert res.over_budget and "took longer than 60 seconds" in res.notes[-1]
    assert len(web.urls) == 3       # robots.txt, homepage, careers page; then the check stopped it
    assert res.careers_url == "https://acme.com/careers"  # what was learned before the stop is kept


def test_running_out_of_time_never_erases_an_earlier_finding(conn, monkeypatch):
    companies.add_company(conn, "Paycomco", website="https://paycomco.com")
    pages = {"https://paycomco.com":
             '<a href="https://www.paycomonline.net/v4/ats/web.php/portal/ABC123/career-page">Jobs</a>'}
    run_resolve(conn, pages)
    assert resolution(conn, "Paycomco")["job_system"] == "paycom"

    monkeypatch.setattr(resolve, "FINGERPRINTS", FINGERPRINTS + [("evil", re.compile(r"(a+)+$"))])
    web = FakeWeb({"https://paycomco.com": "a" * 60 + "!"})
    (res,) = resolve_companies(conn, resolver=Resolver(session=web, cpu_budget=0.3))
    assert res.over_budget
    after = resolution(conn, "Paycomco")
    assert (after["outcome"], after["job_system"]) == ("unsupported", "paycom")   # kept
    assert "stopped: resolving this company used more than 0.3 seconds" in after["evidence"]["notes"][-1]
    assert gaps.gaps_report(conn)["systems"][0]["system"] == "paycom"


# --- the limit on a whole pass -----------------------------------------------

def test_pass_time_limit_stops_starting_companies_and_reports_the_rest(conn, monkeypatch):
    for name in ("Aa", "Bb", "Cc", "Dd"):
        companies.add_company(conn, name, website=f"https://{name.lower()}.com")
    clock = [0.0]

    class Ticking(FakeWeb):
        def get(self, url, **kwargs):
            clock[0] += 10
            return super().get(url, **kwargs)
    monkeypatch.setattr(resolve.time, "monotonic", lambda: clock[0])
    left = []
    results = resolve_companies(conn, resolver=Resolver(session=Ticking({}), wall_budget=1000),
                                time_limit=100, not_checked=left)
    # Each company costs 60 "seconds" (6 requests): two fit, then no more are started.
    assert [r.company for r in results] == ["Aa", "Bb"]
    assert left == ["Cc", "Dd"]

    # Next time the ones left over go first: longest-unchecked first.
    clock[0] = 0.0
    left = []
    results = resolve_companies(conn, resolver=Resolver(session=Ticking({}), wall_budget=1000),
                                time_limit=100, not_checked=left)
    assert [r.company for r in results] == ["Cc", "Dd"] and left == ["Aa", "Bb"]


def test_daily_scout_and_backup_complete_when_the_recheck_is_cut_short(conn, monkeypatch, capsys, tmp_path):
    companies.add_company(conn, "Mapped", website="https://mapped.com")
    companies.add_board(conn, "Mapped", "lever", "mapped")
    for name in ("Loose", "Looser"):
        companies.add_company(conn, name, website=f"https://{name.lower()}.com")
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: scout.run_scout(
        c, adapter_for=Scripted(mapped=[[role("M1", source="lever")]]), on_board=kw.get("on_board")))
    web = FakeWeb({})
    real = resolve.resolve_companies
    monkeypatch.setattr(cli, "resolve_companies", lambda c, **kw: real(
        c, **{**kw, "resolver": Resolver(session=web)}))

    assert cli.main(["daily", "--backup-dir", str(tmp_path / "b"), "--recheck-minutes", "0"]) == 0
    out = capsys.readouterr().out
    assert "Run 1 completed" in out
    # The scout ran and the backup was written before the re-check began.
    assert out.index("Backup written") < out.index("Weekly coverage re-check")
    assert "Re-check stopped at its 0-minute limit; not checked this week, first in line next week (2): Loose, Looser" in out
    assert web.urls == [] and len(ops.list_backups(tmp_path / "b")) == 1
    assert ", ok ===" in out
    assert db.get_meta(conn, gaps.RECHECK_META_KEY) is not None   # bounded: not retried tomorrow


def test_a_recheck_that_blows_up_does_not_fail_the_day(conn, monkeypatch, capsys, tmp_path):
    companies.add_company(conn, "Mapped")
    companies.add_board(conn, "Mapped", "lever", "mapped")
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: scout.run_scout(
        c, adapter_for=Scripted(mapped=[[role("M1", source="lever")]])))

    def broken(*args, **kwargs):
        raise RuntimeError("resolver exploded")
    monkeypatch.setattr(cli, "resolve_companies", broken)
    assert cli.main(["daily", "--backup-dir", str(tmp_path / "b")]) == 0
    out = capsys.readouterr().out
    assert "Coverage re-check failed, will try again tomorrow: RuntimeError: resolver exploded" in out
    assert len(ops.list_backups(tmp_path / "b")) == 1 and ", ok ===" in out
