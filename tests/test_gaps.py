"""Fingerprint detection, hand-recorded job systems, the weekly re-check, gaps and briefs."""

import json
from datetime import datetime, timezone

import pytest

from rolescout import cli, companies, db, gaps, resolve, scout
from rolescout.adapters import SourceError
from rolescout.resolve import (
    Resolver, rank_deeper_links, resolve_companies, scan_for_boards, scan_for_fingerprints,
    set_job_system,
)
from tests.conftest import FakeWeb
from tests.test_resolve import (
    ASHBY, GH, LEVER, ashby_jobs, boards_of, gh_jobs, lever_jobs, resolution, run_resolve,
)
from tests.test_scout import Clock, Scripted, role


# --- fingerprints ------------------------------------------------------------

def test_fingerprint_in_a_script_url_names_the_system(conn):
    """A Workable embed script: listings render in the browser, the script tag is in the HTML."""
    companies.add_company(conn, "Rivet", website="https://rivet.com")
    (res,), web = run_resolve(conn, {
        "https://rivet.com": '<a href="/careers">Careers</a>',
        "https://rivet.com/careers": '''
            <div id="whr_embed_hook"></div>
            <script src="https://www.workable.com/assets/embed.js" type="text/javascript"></script>
            <script>whr(document).ready(function(){ whr_embed(157387, {detail: 'titles'}); });</script>''',
        GH.format("rivet"): gh_jobs(3, content="<p>rivet.com</p>"),  # must not be guessed
    })
    assert (res.outcome, res.job_system, res.job_system_url) == (
        "unsupported", "workable", "https://rivet.com/careers")
    assert res.notes == [
        "identified as workable by 'workable.com/assets/embed.js' on https://rivet.com/careers"]
    saved = resolution(conn, "Rivet")
    assert (saved["job_system"], saved["manual"]) == ("workable", 0)
    assert GH.format("rivet") not in web.urls  # a named system outranks guessing


def test_greenhouse_board_token_held_in_a_script_variable_is_mapped(conn):
    companies.add_company(conn, "Glint", website="https://glint.com")
    (res,), _ = run_resolve(conn, {
        "https://glint.com": '<a href="/careers">Careers</a>',
        "https://glint.com/careers": '''<script>
            const company = "glintwork";
            fetch("https://boards-api.greenhouse.io/v1/boards/" + company + "/departments/")
        </script>''',
        GH.format("glintwork"): gh_jobs(7),
    })
    assert res.outcome == "mapped"
    assert boards_of(conn, "Glint") == [
        ("greenhouse", "glintwork", "link", "linked from https://glint.com/careers")]


def test_board_token_found_in_the_sites_own_script_file(conn):
    companies.add_company(conn, "Adder", website="https://adder.com")
    (res,), web = run_resolve(conn, {
        "https://adder.com": '<a href="/careers">Careers</a>',
        "https://adder.com/careers": '''
            <div data-block-type="greenhouse_job_listing"><section id="greenhouse-jobs"></section></div>
            <script src="https://cdn.other.com/vendor.js"></script>
            <script src="/js/adder.js?abc123"></script>''',
        "https://adder.com/js/adder.js?abc123":
            'fetch("https://api.greenhouse.io/v1/boards/adder1/jobs").then(function(e){return e.json()})',
        GH.format("adder1"): gh_jobs(4),
    })
    assert boards_of(conn, "Adder") == [
        ("greenhouse", "adder1", "link", "linked from https://adder.com/js/adder.js?abc123")]
    assert "https://cdn.other.com/vendor.js" not in web.urls  # only the company's own scripts are read


def test_supported_system_without_a_board_name_asks_for_a_manual_mapping(conn):
    companies.add_company(conn, "Brass", website="https://brass.com")
    (res,), web = run_resolve(conn, {
        "https://brass.com": '<a href="/careers">Careers</a>',
        "https://brass.com/careers": '<a href="/jobs/123?gh_jid=123">Engineer</a>',
        LEVER.format("brass"): lever_jobs(2, "brass.com"),  # another source: not tried
    })
    assert (res.outcome, res.job_system) == ("needs_review", "greenhouse")
    assert 'map it by hand: rolescout companies map "Brass" greenhouse BOARD' in res.notes[-1]
    assert boards_of(conn, "Brass") == []
    # The fingerprint narrows guessing to that one source.
    assert GH.format("brass") in web.urls and LEVER.format("brass") not in web.urls


def test_fingerprint_patterns():
    found = scan_for_fingerprints('''
        <iframe src="https://boards.greenhouse.io/embed/job_board?for=x"></iframe>
        <a href="https://www.paycomonline.net/v4/ats/web.php/portal/2CF04B0F/career-page">Jobs</a>
        <script src="https://ats.rippling.com/embed.js"></script>''')
    assert set(found) == {"greenhouse", "paycom", "rippling"}
    assert scan_for_boards('<a href="https://www.paycomonline.net/v4/ats/web.php/portal/2CF04B0F/career-page">')[1] == {
        "paycom": "https://www.paycomonline.net/v4/ats/web.php/portal/2CF04B0F/career-page"}
    # Marketing copy that merely mentions a vendor is not a fingerprint.
    assert scan_for_fingerprints("We integrate with Greenhouse, Lever, Workable and Rippling.") == {}
    assert scan_for_fingerprints('<a href="https://www.workable.com/">partner</a>') == {}


def test_second_hop_prefers_the_company_page_over_translations():
    links = ["https://wb.ai/site/ko/careers/", "https://wb.ai/site/ja/careers/",
             "https://parentco.com/careers/weights-biases", "https://wb.ai/careers/perks"]
    assert rank_deeper_links(links, "Weights & Biases")[0] == "https://parentco.com/careers/weights-biases"
    assert rank_deeper_links(["https://x.com/de/careers", "https://x.com/careers/open-roles"], "X")[0] == (
        "https://x.com/careers/open-roles")


def test_links_to_other_sites_need_more_than_career_in_the_path():
    from rolescout.resolve import careers_links
    page = '''
        <a href="https://www.wsj.com/lifestyle/careers/ai-entry-level-jobs">Read our CEO in the press</a>
        <a href="https://parentco.com/careers/weights-biases">Open positions at W&amp;B</a>
        <a href="https://wbcareers.com/">Come build with us</a>
        <a href="https://elsewhere.com/x">Careers</a>
        <a href="/company/careers">Work here</a>'''
    assert set(careers_links(page, "https://wb.ai/", {"wb.ai"}, "Weights & Biases")) == {
        "https://parentco.com/careers/weights-biases",   # names the company
        "https://wbcareers.com/",                            # a careers host
        "https://elsewhere.com/x",                        # the link text says Careers
        "https://wb.ai/company/careers"}                  # the company's own site


# --- hand-recorded job systems -----------------------------------------------

def test_manual_entry_survives_re_resolve(conn, capsys):
    companies.add_company(conn, "Ripple", website="https://ripple.com", tier=1)
    assert cli.main(["companies", "set-system", "ripple", "Rippling",
                     "https://ats.rippling.com/ripple/jobs", "--note", "found with a browser"]) == 0
    assert "job system recorded as rippling" in capsys.readouterr().out
    before = resolution(conn, "Ripple")
    assert (before["job_system"], before["job_system_url"], before["manual"]) == (
        "rippling", "https://ats.rippling.com/ripple/jobs", 1)

    pages = {
        "https://ripple.com": '<a href="/careers">Careers</a>',
        "https://ripple.com/careers": '<a href="https://apply.workable.com/ripple/">Jobs</a>',
    }
    # The unattended pass does not even look at it...
    results, web = run_resolve(conn, pages)
    assert results == [] and web.urls == []
    # ...and an explicit re-resolve detects something else but does not overwrite the entry.
    (res,), _ = run_resolve(conn, pages, names=["Ripple"])
    assert res.job_system == "workable"
    assert "recorded by hand" in res.notes[-1]
    assert resolution(conn, "Ripple") == before

    # Clearing it hands the company back to automatic detection.
    assert cli.main(["companies", "clear-system", "Ripple"]) == 0
    run_resolve(conn, pages)
    after = resolution(conn, "Ripple")
    assert (after["job_system"], after["manual"]) == ("workable", 0)


def test_manual_entry_does_not_block_a_real_board_being_found(conn):
    companies.add_company(conn, "Acme", website="https://acme.com")
    set_job_system(conn, "Acme", "icims", "https://acme.icims.com/x")
    (res,), _ = run_resolve(conn, {
        "https://acme.com": '<a href="https://jobs.lever.co/acme">Jobs</a>',
        LEVER.format("acme"): lever_jobs(2),
    }, names=["Acme"])
    assert boards_of(conn, "Acme")[0][:2] == ("lever", "acme")
    assert [e["state"] for e in gaps.classify(conn)] == ["covered"]


def test_set_system_validation(conn, capsys):
    companies.add_company(conn, "Acme")
    assert cli.main(["companies", "set-system", "Acme", "greenhouse", "https://x.com"]) == 1
    assert 'map the board instead with rolescout companies map "Acme" greenhouse BOARD' in capsys.readouterr().err
    assert cli.main(["companies", "set-system", "Acme", "icims", "not-a-url"]) == 1
    assert cli.main(["companies", "set-system", "Nope", "icims", "https://x.com"]) == 1
    capsys.readouterr()
    assert cli.main(["companies", "clear-system", "Acme"]) == 0
    assert capsys.readouterr().out == "Acme has no hand-recorded job system.\n"


# --- gaps report and thresholds ----------------------------------------------

def add(conn, name, tier, system=None, url=None, manual=False):
    companies.add_company(conn, name, website=f"https://{name.lower()}.com", tier=tier)
    if system and manual:
        set_job_system(conn, name, system, url or f"https://{system}.example/{name.lower()}")
    elif system:
        resolve.save_resolution(conn, resolve.Resolution(
            company=name, outcome="unsupported", job_system=system,
            job_system_url=url or f"https://{system}.example/{name.lower()}",
            careers_url=f"https://{name.lower()}.com/careers"))


@pytest.fixture
def gappy(conn):
    # icims: three tier-2/3 companies -> candidate by count
    add(conn, "Wa", 2, "icims"); add(conn, "Wb", 3, "icims"); add(conn, "Wc", None, "icims")
    # rippling: one tier-1 company -> candidate by tier
    add(conn, "Rip", 1, "rippling", manual=True)
    # workable: two tier-2 companies -> below both thresholds
    add(conn, "Ka", 2, "workable"); add(conn, "Kb", 2, "workable")
    # custom: three hand-recorded home-built sites, none tier 1 -> never a candidate by count
    add(conn, "Ca", 2, "custom", manual=True); add(conn, "Cb", 2, "custom", manual=True)
    add(conn, "Cc", 3, "custom", manual=True)
    # an old automatic "custom" row says nothing -> unidentified
    add(conn, "Old", 2, "custom")
    add(conn, "Never", 1)                                          # never resolved -> unidentified
    companies.add_company(conn, "Mapped", tier=1)
    companies.add_board(conn, "Mapped", "lever", "mapped")           # covered
    companies.add_company(conn, "Gone", tier=1)
    companies.set_active(conn, "Gone", False)                       # inactive: not counted
    resolve.save_resolution(conn, resolve.Resolution(              # supported system, no board
        company="Never", outcome="needs_review", job_system="greenhouse",
        job_system_url="https://never.com/careers", notes=["uses greenhouse, board name not found"]))
    add(conn, "Blank", 3)
    return conn


def test_build_candidate_thresholds(gappy):
    report = gaps.gaps_report(gappy)
    groups = {g["system"]: g for g in report["systems"]}
    assert [g["system"] for g in report["systems"]] == ["icims", "rippling", "custom", "workable"]
    assert (groups["icims"]["build_candidate"], groups["icims"]["why"]) == (True, "3 companies")
    assert (groups["rippling"]["build_candidate"], groups["rippling"]["why"]) == (True, "tier 1: Rip")
    assert groups["workable"]["build_candidate"] is False        # two companies, none tier 1
    assert groups["custom"]["build_candidate"] is False          # three, but they share no connector
    assert [(m["company"], m["tier"]) for m in groups["icims"]["companies"]] == [
        ("Wa", 2), ("Wb", 3), ("Wc", None)]
    assert report["build_candidates"] == 2
    assert [e["company"] for e in report["needs_mapping"]] == ["Never"]
    assert [(e["company"], e["reason"]) for e in report["unidentified"]] == [
        ("Old", "careers page names no job system we recognize"), ("Blank", "not resolved yet")]
    assert (report["active_companies"], report["covered"]) == (13, 1)
    assert gaps.summary_line(report) == "2 build candidates, 2 unidentified, 1 awaiting mapping"

    # A third workable company tips it over; a tier-1 custom site is flagged on its own.
    add(gappy, "Kc", 3, "workable")
    add(gappy, "Cd", 1, "custom", manual=True)
    groups = {g["system"]: g for g in gaps.gaps_report(gappy)["systems"]}
    assert groups["workable"]["why"] == "3 companies"
    assert groups["custom"]["why"] == "tier 1: Cd"


def test_gaps_command_and_status_line(gappy, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.main(["gaps"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Coverage gaps: 2 build candidates, 2 unidentified, 1 awaiting mapping "
                          "(12 of 13 active companies not covered)\n")
    candidates, rest = out.split("Other unsupported job systems:")
    assert "icims" in candidates and "rippling" in candidates and "(by hand)" in candidates
    assert "workable" not in candidates and "workable" in rest
    assert "Needs identification - rolescout companies set-system NAME SYSTEM URL" in out
    assert "On a supported system, not mapped yet" in out

    assert cli.main(["gaps", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["build_candidates"] == 2

    # status shows one line, and gaps never change its exit code.
    scripted = Scripted(mapped=[[role("M1", source="lever")]])
    scout.run_scout(gappy, adapter_for=scripted)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("HEALTHY\n")
    assert "Coverage gaps:  2 build candidates, 2 unidentified, 1 awaiting mapping  (rolescout gaps)" in out


def test_connector_brief(gappy, capsys):
    assert cli.main(["brief", "Icims"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Connector brief: icims\n")
    assert "cannot fetch 3 target companies" in out and "Build candidate: yes (3 companies)." in out
    assert "### Wa (tier 2)" in out and "### Wc (no tier)" in out
    assert "- Careers page: https://wa.com/careers" in out
    assert "- Job system URL: https://icims.example/wa" in out
    assert "- Identified: automatically" in out
    assert "## What the adapter must do" in out and "`board_url(ref)`" in out

    assert cli.main(["brief", "rippling"]) == 0
    out = capsys.readouterr().out
    assert "Build candidate: yes (tier 1: Rip)." in out and "- Identified: by hand" in out

    assert cli.main(["brief", "taleo"]) == 1
    assert "Systems with gaps: icims, rippling, custom, workable" in capsys.readouterr().err


# --- the weekly re-check -----------------------------------------------------

def at(day, hour=7):
    return datetime(2026, 3, day, hour, tzinfo=timezone.utc)


def test_weekly_recheck_trigger(conn):
    assert gaps.recheck_due(conn, 7, at(1)) is True            # never checked: due
    gaps.mark_rechecked(conn, "2026-03-01T07:00:00Z")
    assert gaps.recheck_due(conn, 7, at(2)) is False
    assert gaps.recheck_due(conn, 7, at(7)) is False           # six days later: not yet
    assert gaps.recheck_due(conn, 7, at(8, 6)) is True         # the 7th daily run, even if a little early
    assert gaps.recheck_due(conn, 7, at(20)) is True
    assert gaps.recheck_due(conn, 0, at(20)) is False          # turned off
    assert gaps.recheck_due(conn, 1, at(2)) is True


def daily(monkeypatch, conn, script, pages, *argv):
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: scout.run_scout(
        c, adapter_for=script, on_board=kw.get("on_board")))
    web = FakeWeb(pages)
    real = resolve.resolve_companies
    monkeypatch.setattr(cli, "resolve_companies", lambda c, **kw: real(
        c, **{**kw, "resolver": Resolver(session=web)}))
    return cli.main(["daily", *argv]), web


def test_daily_job_rechecks_once_a_week_not_every_day(conn, monkeypatch, capsys):
    companies.add_company(conn, "Mapped", website="https://mapped.com")
    companies.add_board(conn, "Mapped", "lever", "mapped")
    companies.add_company(conn, "Loose", website="https://loose.com")
    script = Scripted(mapped=[[role("M1", source="lever")]] * 3)
    pages = {"https://loose.com": '<a href="https://jobs.ashbyhq.com/loose">Jobs</a>',
             ASHBY.format("loose"): ashby_jobs(2)}

    code, web = daily(monkeypatch, conn, script, pages)
    out = capsys.readouterr().out
    assert code == 0 and "Weekly coverage re-check" in out
    assert "Loose ... saved ashby/loose" in out and "Coverage gaps: 0 build candidates, 0 unidentified" in out
    assert boards_of(conn, "Loose")[0][:2] == ("ashby", "loose")
    assert "https://mapped.com" not in web.urls              # healthy mapped companies are left alone
    assert db.get_meta(conn, gaps.RECHECK_META_KEY) is not None

    script.boards["loose"] = [[role("L1", source="ashby")]]
    code, web = daily(monkeypatch, conn, script, pages)     # the next day
    assert "Weekly coverage re-check" not in capsys.readouterr().out and web.urls == []

    script.boards["loose"] = [[role("L1", source="ashby")]]
    db.set_meta(conn, gaps.RECHECK_META_KEY, "2020-01-01T00:00:00Z")
    code, web = daily(monkeypatch, conn, script, pages)     # a week later
    out = capsys.readouterr().out
    assert "Weekly coverage re-check" in out and "Nothing to re-check." in out


def test_recheck_is_skipped_when_the_scout_could_not_reach_anything(conn, monkeypatch, capsys):
    companies.add_company(conn, "Mapped", website="https://mapped.com")
    companies.add_board(conn, "Mapped", "lever", "mapped")
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    code, web = daily(monkeypatch, conn, Scripted(mapped=[SourceError("offline")] * 3), {})
    assert code == 1 and web.urls == []
    assert "Weekly coverage re-check" not in capsys.readouterr().out
    assert db.get_meta(conn, gaps.RECHECK_META_KEY) is None  # still due tomorrow


def test_failing_board_is_re_resolved_and_the_move_is_recorded(conn, monkeypatch, capsys):
    """Three failed runs in a row: the company probably changed job systems."""
    companies.add_company(conn, "Mover", website="https://mover.com", tier=1)
    companies.add_board(conn, "Mover", "greenhouse", "mover")
    companies.add_company(conn, "Hopper", website="https://hopper.com")
    companies.add_board(conn, "Hopper", "lever", "hopper")
    companies.add_company(conn, "Flaky", website="https://flaky.com")
    companies.add_board(conn, "Flaky", "lever", "flaky")
    companies.add_company(conn, "Steady", website="https://steady.com")
    companies.add_board(conn, "Steady", "lever", "steady")
    gone = SourceError("no board found")
    script = Scripted(
        steady=[[role("S1", source="lever")]] * 4,
        mover=[[role("M1")], gone, gone, gone],
        hopper=[[role("H1", source="lever")], gone, gone, gone],
        flaky=[[role("F1", source="lever")], [role("F1", source="lever")], gone, gone])
    clock = Clock()
    for _ in range(3):
        clock.start()
        scout.run_scout(conn, adapter_for=script, clock=clock)
    gaps.mark_rechecked(conn, "2020-01-01T00:00:00Z")  # a week is up

    pages = {
        # Mover now sends applicants to iCIMS; Hopper moved to an Ashby board.
        "https://mover.com": '<a href="https://careers-mover.icims.com/jobs">Careers</a>',
        "https://hopper.com": '<a href="https://jobs.ashbyhq.com/hopper">Jobs</a>',
        ASHBY.format("hopper"): ashby_jobs(3),
        "https://flaky.com": '<a href="https://jobs.lever.co/flaky">Jobs</a>',
    }
    code, web = daily(monkeypatch, conn, script, pages)   # the fourth run: third failure in a row
    out = capsys.readouterr().out
    assert "includes boards failing three or more runs in a row (Hopper lever/hopper, Mover greenhouse/mover)" in out
    assert "https://flaky.com" not in web.urls              # two failures: not yet

    # Mover: the dead board stays mapped, but the gap is now on record.
    assert resolution(conn, "Mover")["job_system"] == "icims"
    report = gaps.gaps_report(conn)
    (group,) = report["systems"]
    assert (group["system"], group["build_candidate"], group["why"]) == ("icims", True, "tier 1: Mover")
    assert group["companies"][0]["failing_boards"] == ["greenhouse/mover"]

    # Hopper: the new board is saved; the old one is left for a person to remove.
    assert [b[:2] for b in boards_of(conn, "Hopper")] == [("lever", "hopper"), ("ashby", "hopper")]
    assert 'remove it with: rolescout companies unmap "Hopper" lever hopper' in out
    states = {e["company"]: e["state"] for e in gaps.classify(conn)}
    assert states == {"Mover": "unsupported", "Hopper": "covered", "Flaky": "covered", "Steady": "covered"}


def test_recheck_command(conn, monkeypatch, capsys):
    companies.add_company(conn, "Loose", website="https://loose.com")
    web = FakeWeb({"https://loose.com": '<a href="https://jobs.lever.co/loose">Jobs</a>',
                   LEVER.format("loose"): lever_jobs(1)})
    real = resolve.resolve_companies
    monkeypatch.setattr(cli, "resolve_companies", lambda c, **kw: real(
        c, **{**kw, "resolver": Resolver(session=web)}))
    assert cli.main(["companies", "recheck", "--dry-run"]) == 0
    assert db.get_meta(conn, gaps.RECHECK_META_KEY) is None and boards_of(conn, "Loose") == []
    assert cli.main(["companies", "recheck"]) == 0
    assert db.get_meta(conn, gaps.RECHECK_META_KEY) is not None
    assert boards_of(conn, "Loose")[0][:2] == ("lever", "loose")
    capsys.readouterr()


# --- schema migration and export ---------------------------------------------

def test_version_1_database_is_upgraded_in_place(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", 1)
    old = db.connect(path)
    assert db.schema_version(old) == 1
    old.execute("INSERT INTO companies (name, created_at) VALUES ('Acme', 'x')")
    old.execute("INSERT INTO company_resolutions (company_id, resolved_at, outcome, job_system, evidence) "
                "VALUES (1, 'x', 'unsupported', 'icims', '{}')")
    old.close()
    monkeypatch.undo()

    conn = db.connect(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION >= 2
    row = conn.execute("SELECT job_system, manual FROM company_resolutions").fetchone()
    assert tuple(row) == ("icims", 0)                       # existing findings kept, not manual
    assert db.get_meta(conn, "anything") is None
    assert gaps.gaps_report(conn)["systems"][0]["system"] == "icims"


def test_manual_entries_travel_with_the_export(conn, tmp_path):
    from rolescout.transfer import export_companies, import_companies
    companies.add_company(conn, "Rip", website="https://rip.com", tier=1)
    set_job_system(conn, "Rip", "rippling", "https://ats.rippling.com/rip/jobs")
    other = db.connect(tmp_path / "other.db")
    import_companies(other, json.loads(json.dumps(export_companies(conn))))
    assert resolution(other, "Rip")["manual"] == 1
    assert run_resolve(other, {})[0] == []                    # still protected on the other machine
