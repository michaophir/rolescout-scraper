"""The ranked shortlist: config, scoring, marks, history and the CLI views."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rolescout import cli, companies, db, scout
from rolescout import shortlist as ranking
from rolescout.db import transaction
from rolescout.models import Role
from rolescout.scout import apply_fetch
from tests.test_scout import Clock, Scripted

REPO = Path(__file__).parent.parent

SMALL_CONFIG = """
size = 3

[tier_points]
1 = 30
2 = 20

[title]
exclude = ["Intern"]

[title.wanted]
"Product Manager" = 30
"Group Product Manager" = 40

[seniority.words]
"Senior" = 10
"Staff" = 15

[location]
remote_points = 5

[location.preferred]
"New York" = 10

[recency]
max_points = 10
full_days = 1
zero_days = 11
"""


def mk(job_id, title, location="Austin", workplace="", source="greenhouse", posted="", text=None,
       department="Product"):
    """A role. Its description is its own ("About a1") unless ``text`` gives
    one; ``text=""`` stores none."""
    return Role(source=source, source_job_id=job_id, company="x", title=title, department=department,
                location=location, workplace_type=workplace, url=f"https://example.com/{job_id}",
                date_posted=posted, compensation_text="",
                description=f"About {job_id}" if text is None else text)


def day(n, hour=12):
    """Noon UTC on 2026-03-n, as an aware datetime."""
    return datetime(2026, 3, n, hour, tzinfo=timezone.utc)


def stamp(n):
    return f"2026-03-{n:02d}T08:00:00Z"


@pytest.fixture
def world(conn):
    """Two companies with boards; returns a function that records a fetch."""
    ids = {}
    for name, tier in (("Alpha", 1), ("Beta", 2), ("Gamma", None)):
        company_id = companies.add_company(conn, name)
        ids[name] = (company_id, companies.add_board(conn, name, "greenhouse", name.lower()))

    def fetch(company, roles, on_day):
        with transaction(conn):
            return apply_fetch(conn, *ids[company], roles, stamp(on_day))
    return fetch


def cfg(text=SMALL_CONFIG):
    return ranking.parse_config(text, "test")


def listed(conn):
    current = ranking.latest_shortlist(conn)
    return [(e["rank"], e["title"], e["score"]) for e in ranking.shortlist_entries(conn, current["id"])]


def role_id(conn, job_id):
    return conn.execute("SELECT id FROM roles WHERE source_job_id = ?", (job_id,)).fetchone()[0]


# --- ranking -----------------------------------------------------------------

def test_ranking_order_from_a_small_config(conn, world):
    world("Alpha", [
        mk("a1", "Senior Product Manager, Growth", "New York, NY"),   # 30 title + 10 senior + 10 NY + 10 new
        mk("a2", "Product Manager"),                                  # 30 + 10 new
        mk("a3", "Staff Software Engineer", "New York"),              # no wanted title: never listed
    ], on_day=10)
    world("Beta", [
        mk("b1", "Group Product Manager", "Remote - US"),             # 40 (best phrase) + 5 remote + 10 new
        mk("b2", "Product Manager", workplace="remote"),              # 30 + 5 + 10
    ], on_day=10)
    world("Gamma", [mk("g1", "Staff Product Manager")], on_day=10)   # 30 + 15 + 10

    result = ranking.build_shortlist(conn, cfg(), now=day(10))
    assert (result["listed"], result["eligible"], result["new"]) == (3, 5, None)
    assert listed(conn) == [
        (1, "Senior Product Manager, Growth", 60.0),
        (2, "Group Product Manager", 55.0),
        (3, "Staff Product Manager", 55.0),        # a company's tier earns nothing; size = 3 cuts the rest
    ]
    (top,) = [e for e in ranking.shortlist_entries(conn, result["shortlist_id"]) if e["rank"] == 1]
    assert top["reasons"] == [
        'Title matches "Product Manager" (+30)',
        'Seniority: "Senior" (+10)', 'Location matches "New York" (+10)', "First seen today (no posted date) (+10)"]
    assert (top["company"], top["id"]) == ("Alpha", role_id(conn, "a1")) and "tier" not in top


def test_phrases_match_whole_words_only():
    c = cfg()
    base = {"location": "", "workplace_type": "", "first_seen_at": stamp(10), "tier": 1, "date_posted": ""}
    def ev(title):
        return ranking.evaluate({**base, "title": title}, c, day(10).date())
    assert ev("Product Manager (Technical)").eligible
    assert ev("senior   product manager").eligible                 # case and spacing do not matter
    assert not ev("Product Marketing Manager").eligible
    assert not ev("Product Managers Assistant").eligible
    assert not ev("Byproduct Manager").eligible
    assert ev("Product Manager").score == ev("Product Manager, Seniority Programs").score  # "Senior" != "Seniority"


def test_recency_fades_evenly():
    c = cfg()
    role = {"title": "Product Manager", "location": "", "workplace_type": "", "tier": None, "date_posted": ""}
    def points(first_seen_day, today):
        return ranking.evaluate({**role, "first_seen_at": stamp(first_seen_day)}, c, day(today).date()).score - 30
    assert [points(1, 1), points(1, 2), points(1, 7), points(1, 12), points(1, 30)] == [10, 10, 5, 0, 0]


def test_recency_counts_from_the_posted_date_when_there_is_one():
    c = cfg()   # 10 points up to 1 day old, fading to 0 at 11 days
    role = {"title": "Product Manager", "location": "", "workplace_type": "", "tier": None,
            "first_seen_at": stamp(10)}
    def ev(posted, baseline=False):
        return ranking.evaluate({**role, "date_posted": posted}, c, day(10).date(), baseline)
    # Seen for the first time today, but posted six days ago: scored as six days old.
    old = ev("2026-03-04")
    assert old.score == 30 + 5 and old.reasons[-1] == "Posted 6 days ago (+5)"
    assert ev("2026-03-10").reasons[-1] == "Posted today (+10)"
    assert ev("2026-01-01").detail[-1] == "Posted 68 days ago (+0)"
    # No posted date: first seen is used...
    assert ev("").reasons[-1] == "First seen today (no posted date) (+10)"
    # ...unless the role came with the baseline fetch: then its age is unknown.
    unknown = ev("", baseline=True)
    assert unknown.score == 30
    assert unknown.detail[-1] == ("No posted date, and it was already open when the company was first "
                                  "fetched, so its age is unknown (+0)")
    assert ev("2026-03-09", baseline=True).reasons[-1] == "Posted yesterday (+10)"   # a posted date still counts
    # Dates that cannot be right fall back to first seen.
    assert ev("not a date").reasons[-1] == "First seen today (no posted date) (+10)"
    assert ev("2031-01-01").reasons[-1] == "First seen today (no posted date) (+10)"


def test_baseline_roles_are_not_new_but_later_ones_are(conn):
    companies.add_company(conn, "Alpha")
    companies.add_board(conn, "Alpha", "greenhouse", "alpha")
    script = Scripted(alpha=[
        [mk("old", "Product Manager"), mk("dated", "Product Manager", posted="2025-12-31")],
        [mk("old", "Product Manager"), mk("dated", "Product Manager", posted="2025-12-31"),
         mk("fresh", "Product Manager")]])
    clock = Clock()                              # run 1 on 2026-01-01, run 2 on 2026-01-02
    for _ in range(2):
        clock.start()
        scout.run_scout(conn, adapter_for=script, clock=clock)
    ranking.build_shortlist(conn, cfg(), now=datetime(2026, 1, 2, 12, tzinfo=timezone.utc))
    entries = {e["id"]: e for e in ranking.shortlist_entries(conn, ranking.latest_shortlist(conn)["id"])}
    by_job = {job: entries[role_id(conn, job)] for job in ("old", "dated", "fresh")}
    assert by_job["fresh"]["score"] == 40 and by_job["fresh"]["rank"] == 1       # +10: seen after the baseline
    assert by_job["old"]["score"] == 30                                          # baseline, no date: no recency
    assert by_job["dated"]["score"] == 39                                        # posted 2 days ago: 9 of 10
    assert not any("First seen" in reason for reason in by_job["old"]["reasons"])


# --- one job, several postings -------------------------------------------------

ONE_SLOT = SMALL_CONFIG.replace("size = 3", "size = 3\nmax_per_company = 1")


def about(place):
    """One job's description as each city's posting words it: the place and
    the line breaks differ, nothing else does."""
    return f"Location: {place}\nLead the roadmap.\n\n  This role is based in {place}. Ship   weekly."


def per_city(world):
    world("Alpha", [
        mk("ny", "Senior Product Manager", "New York, NY", text=about("New York, NY")),   # 30 + 10 + 10 NY + 10 new = 60
        mk("austin", "Senior Product Manager", "Austin, TX", text=about("Austin, TX")),   # 50
        # 55; capitals and spacing aside, the same title, and the text on one line.
        mk("remote", "senior  product manager", "Remote - US", text=about("Remote - US").replace("\n", " ")),
        mk("other", "Product Manager", "Austin, TX"),                  # 40, another job
    ], on_day=10)
    # The same title and the same text at another company: 50, and another job.
    world("Beta", [mk("b1", "Senior Product Manager", "Austin, TX", text=about("Austin, TX"))], on_day=10)


def test_a_job_posted_once_per_city_is_one_entry_scored_by_its_best_location(conn, world):
    per_city(world)
    result = ranking.build_shortlist(conn, cfg(ONE_SLOT), now=day(10))
    # Three jobs qualify, not five postings. Alpha's job with three postings
    # takes its one slot, so its other job is what the cap holds back.
    assert (result["listed"], result["eligible"], result["held_back"]) == (2, 3, 1)
    current = ranking.latest_shortlist(conn)["id"]
    first, second = ranking.shortlist_entries(conn, current, cfg(ONE_SLOT))
    assert (first["company"], first["id"], first["score"]) == ("Alpha", role_id(conn, "ny"), 60.0)
    assert first["location"] == "New York, NY; Remote - US; Austin, TX"       # the best first, then by score
    assert first["location_shown"] == "New York, NY +2 more"
    assert first["postings"] == 3
    assert [(o["id"], o["location"], o["score"]) for o in first["other_postings"]] == [
        (role_id(conn, "remote"), "Remote - US", 55.0), (role_id(conn, "austin"), "Austin, TX", 50.0)]
    assert (second["company"], second["postings"], second["other_postings"]) == ("Beta", 1, [])
    assert second["location"] == second["location_shown"] == "Austin, TX"
    held = ranking.held_back(conn, current)
    assert [(h["title"], h["postings"]) for h in held] == [("Product Manager", 1)]
    # Every posting is still stored, open and its own role.
    assert conn.execute("SELECT COUNT(*) FROM roles WHERE closed_at IS NULL").fetchone()[0] == 5

    # A posting that is not the entry explains itself by the entry it is listed with.
    info = ranking.explain(conn, role_id(conn, "austin"), cfg(ONE_SLOT), now=day(10))
    assert (info["rank"], info["why_not"], info["score"], info["listed_score"]) == (1, "", 50.0, 50.0)
    assert info["listed_under"] == {"id": role_id(conn, "ny"), "location": "New York, NY",
                                    "url": "https://example.com/ny", "score": 60.0}
    lead = ranking.explain(conn, role_id(conn, "ny"), cfg(ONE_SLOT), now=day(10))
    assert lead["listed_under"] is None and [o["id"] for o in lead["other_postings"]] == [
        role_id(conn, "remote"), role_id(conn, "austin")]

    # With no cap the held-back job is listed; the three postings are still one entry.
    ranking.build_shortlist(conn, cfg(), now=day(10))
    assert [(rank, title) for rank, title, _ in listed(conn)] == [
        (1, "Senior Product Manager"), (2, "Senior Product Manager"), (3, "Product Manager")]


def test_the_same_job_led_by_another_city_is_not_new_and_a_mark_covers_the_job(conn, world, capsys, isolated_db_path):
    per_city(world)
    (isolated_db_path.parent / "ranking.toml").write_text(ONE_SLOT)
    ranking.build_shortlist(conn, cfg(ONE_SLOT), now=day(10))
    # The next day the New York posting moves to Boston and scores less: the
    # remote posting leads the entry now. It is the same entry, not a new one.
    world("Alpha", [mk("ny", "Senior Product Manager", "Boston, MA", text=about("Boston, MA")),
                    mk("austin", "Senior Product Manager", "Austin, TX", text=about("Austin, TX")),
                    mk("remote", "senior  product manager", "Remote - US", text=about("Remote - US")),
                    mk("other", "Product Manager", "Austin, TX")], on_day=11)
    world("Beta", [mk("b1", "Senior Product Manager", "Austin, TX", text=about("Austin, TX"))], on_day=11)
    result = ranking.build_shortlist(conn, cfg(ONE_SLOT), now=day(11))
    current = ranking.latest_shortlist(conn)["id"]
    first = ranking.shortlist_entries(conn, current, cfg(ONE_SLOT))[0]
    assert (first["id"], first["score"], first["location_shown"]) == (role_id(conn, "remote"), 55.0, "Remote - US +2 more")
    assert result["new"] == 0 and ranking.new_since(conn, current)[1] == set()

    assert cli.main(["shortlist"]) == 0
    out = capsys.readouterr().out
    assert "2 roles of 3 that qualify" in out and "Remote - US +2 more" in out
    assert "A job posted once per location is one entry" in out
    assert cli.main(["why", str(role_id(conn, "austin"))]) == 0
    out = capsys.readouterr().out
    assert "Rank 1 of 2 on the shortlist" in out
    assert f"The entry is this job's best-scoring posting, ID {role_id(conn, 'remote')} (Remote - US, score 55)" in out

    # Saved under any of its postings, the job is flagged saved.
    ranking.mark_role(conn, role_id(conn, "austin"), "saved")
    assert ranking.shortlist_entries(conn, current)[0]["mark"] == "saved"
    ranking.unmark_role(conn, role_id(conn, "austin"))

    # Dismissing the entry dismisses the job: no other city's posting takes
    # its place, and Alpha's slot goes to its other job.
    assert cli.main(["mark", str(role_id(conn, "remote")), "dismissed"]) == 0
    out = capsys.readouterr().out
    assert (f"The same job is posted 2 more times (ID {role_id(conn, 'ny')}, {role_id(conn, 'austin')}); "
            "the mark covers the job, so those stay off the shortlist too.") in out
    assert [(e["company"], e["title"]) for e in ranking.shortlist_entries(
        conn, ranking.latest_shortlist(conn)["id"])] == [("Beta", "Senior Product Manager"), ("Alpha", "Product Manager")]
    info = ranking.explain(conn, role_id(conn, "ny"), cfg(ONE_SLOT), now=day(11))
    assert info["rank"] is None and info["why_not"] == (
        f"you marked another posting of the same job dismissed (ID {role_id(conn, 'remote')}, Remote - US), "
        "and a mark covers the job wherever it is posted")
    # Beta's job has the same title and is another company's: untouched.
    assert ranking.explain(conn, role_id(conn, "b1"), cfg(ONE_SLOT), now=day(11))["rank"] == 1
    assert cli.main(["unmark", str(role_id(conn, "remote"))]) == 0
    assert ranking.shortlist_entries(conn, ranking.latest_shortlist(conn)["id"])[0]["postings"] == 3


def entries_of(conn, config=None):
    """Today's entries as (the posting that leads, the postings folded under it), by source job ID."""
    names = {r["id"]: r["source_job_id"] for r in conn.execute("SELECT id, source_job_id FROM roles")}
    ranking.build_shortlist(conn, config or cfg(WIDE), now=day(10))
    return [(names[e["id"]], sorted(names[o["id"]] for o in e["other_postings"]))
            for e in ranking.shortlist_entries(conn, ranking.latest_shortlist(conn)["id"])]


WIDE = SMALL_CONFIG.replace("size = 3", "size = 20\nmax_per_company = 0")


def test_the_same_title_is_not_enough_to_fold_two_postings(conn, world):
    # A large company posts many jobs under one title. Only postings whose
    # description is the same text, the place and the spacing aside, are one job.
    world("Alpha", [
        mk("ads-ny", "Product Manager", "New York, NY", text="Own the ads roadmap. Based in New York, NY."),
        mk("ads-sf", "Product Manager", "San Francisco, CA", text="Own the ads  roadmap.\nBased in San Francisco, CA."),
        mk("pay-ny", "Product Manager", "New York, NY", text="Own the payments roadmap. Based in New York, NY."),
        mk("pay-sf", "Product Manager", "San Francisco, CA", text="Own the payments roadmap. Based in San Francisco, CA."),
        mk("search", "Product Manager", "New York, NY", text="Own the search roadmap."),
        # One text that names every city it is posted for is the same text.
        mk("all-ny", "Product Manager", "New York, NY", text="Growth. Offices: New York, NY and Austin, TX."),
        mk("all-tx", "Product Manager", "Austin, TX", text="Growth. Offices: New York, NY and Austin, TX."),
        # The place is all that may differ: another word is another job.
        mk("team-a", "Product Manager", "Denver, CO", text="Join team A in Denver, CO."),
        mk("team-b", "Product Manager", "Denver, CO", text="Join team B in Denver, CO."),
    ], on_day=10)
    assert sorted(entries_of(conn)) == [
        ("ads-ny", ["ads-sf"]), ("all-ny", ["all-tx"]), ("pay-ny", ["pay-sf"]),
        ("search", []), ("team-a", []), ("team-b", [])]
    # Five postings that qualify beyond those folded, each its own entry with its own slot.
    assert ranking.latest_shortlist(conn)["eligible"] == 6
    capped = entries_of(conn, cfg(WIDE.replace("max_per_company = 0", "max_per_company = 2")))
    assert len(capped) == 2 and len(ranking.held_back(conn, ranking.latest_shortlist(conn)["id"])) == 4


def test_without_a_description_to_compare_the_posted_date_and_department_decide(conn, world):
    def bare(job_id, posted="2026-03-08", department="Product", text="", place="Austin, TX"):
        return mk(job_id, "Product Manager", place, posted=posted, department=department, text=text)
    world("Alpha", [
        # No description stored for either: the same date and department make them one job.
        bare("a1"), bare("a2", place="Denver, CO"),
        bare("day", posted="2026-03-07"),                       # another day: another job
        bare("dept", department="Growth"),                      # another department: another job
        bare("undated", posted=""), bare("undated-2", posted=""),               # no date to go by: in doubt, apart
        bare("no-dept", department=""), bare("no-dept-2", department=""),       # no department to go by: apart
    ], on_day=10)
    assert sorted(entries_of(conn)) == [
        ("a1", ["a2"]), ("day", []), ("dept", []), ("no-dept", []), ("no-dept-2", []),
        ("undated", []), ("undated-2", [])]

    # One with a description and one without are compared by date and department too.
    world("Beta", [bare("b1", text="Own the roadmap."), bare("b2", place="Denver, CO"),
                   bare("b3", posted="2026-03-01")], on_day=10)
    assert [e for e in entries_of(conn) if e[0].startswith("b")] == [("b1", ["b2"]), ("b3", [])]
    # Two descriptions that differ settle it, whatever the date and the
    # department say; and a posting joins a job only if it matches all of it.
    world("Gamma", [bare("g1", text="Own ads."), bare("g2", text="Own search."), bare("g3")], on_day=10)
    assert [e for e in entries_of(conn) if e[0].startswith("g")] == [("g1", ["g3"]), ("g2", [])]


def test_a_mark_covers_only_the_postings_folded_with_it(conn, world):
    world("Alpha", [
        mk("ads-ny", "Product Manager", "New York, NY", text="Own the ads roadmap. Based in New York, NY."),
        mk("ads-sf", "Product Manager", "San Francisco, CA", text="Own the ads roadmap. Based in San Francisco, CA."),
        mk("pay-ny", "Product Manager", "New York, NY", text="Own the payments roadmap. Based in New York, NY."),
    ], on_day=10)
    for status in ("dismissed", "applied"):
        ranking.mark_role(conn, role_id(conn, "ads-ny"), status)
        assert [o["id"] for o in ranking.other_open_postings(conn, ranking.get_role(conn, role_id(conn, "ads-ny")))] == [
            role_id(conn, "ads-sf")]
        # The other city's posting of the same job goes; the other job with the same title stays.
        assert entries_of(conn) == [("pay-ny", [])]
        info = ranking.explain(conn, role_id(conn, "ads-sf"), cfg(WIDE), now=day(10))
        assert info["why_not"].startswith(f"you marked another posting of the same job {status} "
                                          f"(ID {role_id(conn, 'ads-ny')}, New York, NY)")
        assert ranking.explain(conn, role_id(conn, "pay-ny"), cfg(WIDE), now=day(10))["rank"] == 1
        ranking.unmark_role(conn, role_id(conn, "ads-ny"))
    assert sorted(entries_of(conn)) == [("ads-ny", ["ads-sf"]), ("pay-ny", [])]


def test_a_filtered_ranking_folds_the_postings_that_pass_the_filter(conn, world):
    from rolescout import filters
    per_city(world)
    ranking.build_shortlist(conn, cfg(ONE_SLOT), now=day(10))
    found = ranking.filtered_entries(conn, cfg(ONE_SLOT), filters.build_filter(conn, location="Austin"), now=day(10))
    # Of Alpha's three postings only Austin passes; the entry is that posting,
    # and it still knows its rank on today's list, where New York leads it.
    assert found["qualifying"] == 3
    assert [(e["company"], e["title"], e["id"], e["location_shown"], e["postings"], e["list_rank"])
            for e in found["entries"]] == [
        ("Alpha", "Senior Product Manager", role_id(conn, "austin"), "Austin, TX", 1, 1),
        ("Beta", "Senior Product Manager", role_id(conn, "b1"), "Austin, TX", 1, 2),
        ("Alpha", "Product Manager", role_id(conn, "other"), "Austin, TX", 1, None)]
    usa = ranking.filtered_entries(conn, cfg(ONE_SLOT), filters.build_filter(conn, location="United States"),
                                   now=day(10))["entries"]
    assert (usa[0]["id"], usa[0]["postings"], usa[0]["location_shown"]) == (
        role_id(conn, "ny"), 3, "New York, NY +2 more")
    assert len(usa) == 3                                          # no cap in a filtered ranking


# --- ties ----------------------------------------------------------------------

NO_RECENCY = SMALL_CONFIG.replace("size = 3", "size = 10\nmax_per_company = 1").replace("max_points = 10", "max_points = 0")


def test_roles_with_the_same_score_are_ordered_by_posted_date_then_company(conn, world):
    # Every role scores 30. Entered in an order that is none of the orders below.
    world("Gamma", [mk("g1", "Product Manager", posted="2026-03-05")], on_day=9)
    world("Beta", [mk("b1", "Product Manager", posted="2026-03-08")], on_day=9)
    world("Alpha", [
        mk("a-old", "Product Manager, Ads", posted="2026-03-07"),
        mk("a-zulu", "Product Manager, Zulu", posted="2026-03-08"),
        mk("a-seen", "Product Manager, Billing"),                    # no posted date: first seen 03-09 stands in
        mk("a-mid", "Product Manager, Cloud", posted="2026-03-07"),
    ], on_day=9)

    def order():
        return [(role["company"], role["title"]) for role, ev in ranking.rank_roles(conn, cfg(NO_RECENCY), now=day(10))]
    assert {ev.score for _, ev in ranking.rank_roles(conn, cfg(NO_RECENCY), now=day(10))} == {30.0}
    expected = [
        ("Alpha", "Product Manager, Billing"),       # 03-09, the newest
        ("Alpha", "Product Manager, Zulu"),          # 03-08: Alpha before Beta
        ("Beta", "Product Manager"),                 # 03-08
        ("Alpha", "Product Manager, Ads"),           # 03-07: the same company, so by title
        ("Alpha", "Product Manager, Cloud"),         # 03-07
        ("Gamma", "Product Manager"),                # 03-05
    ]
    assert order() == expected and order() == expected

    # The list and the roles the cap holds back keep that order.
    ranking.build_shortlist(conn, cfg(NO_RECENCY), now=day(10))
    current = ranking.latest_shortlist(conn)["id"]
    assert [(e["company"], e["title"]) for e in ranking.shortlist_entries(conn, current)] == [
        expected[0], expected[2], expected[5]]
    assert [(h["company"], h["title"]) for h in ranking.held_back(conn, current)] == [
        expected[1], expected[3], expected[4]]

    # A role already open when its board was first read, with no posted date,
    # has no date at all and comes after every role that has one.
    companies.add_company(conn, "Delta")
    companies.add_board(conn, "Delta", "greenhouse", "delta")
    scout.run_scout(conn, adapter_for=Scripted(delta=[[mk("d1", "Product Manager")]]), clock=lambda: stamp(10))
    assert order() == [*expected, ("Delta", "Product Manager")]


# --- the per-company cap -----------------------------------------------------

CAPPED = SMALL_CONFIG.replace("size = 3", "size = 4\nmax_per_company = 2")


def test_per_company_cap_holds_back_the_extra_roles(conn, world, capsys, isolated_db_path):
    world("Alpha", [mk("a1", "Staff Product Manager"), mk("a2", "Senior Product Manager"),
                    mk("a3", "Senior Product Manager, Growth"), mk("a4", "Product Manager")], on_day=10)
    world("Beta", [mk("b1", "Product Manager")], on_day=10)
    world("Gamma", [mk("g1", "Product Manager")], on_day=10)
    config = isolated_db_path.parent / "ranking.toml"
    config.write_text(CAPPED)
    assert cli.main(["shortlist", "rebuild"]) == 0
    assert "4 roles (of 6 that qualify), 2 held back by the per-company cap" in capsys.readouterr().out
    # Alpha's two best take its slots; lower-scoring roles of other companies fill the rest.
    assert [(e["company"], e["title"]) for e in ranking.shortlist_entries(
        conn, ranking.latest_shortlist(conn)["id"])] == [
        ("Alpha", "Staff Product Manager"), ("Alpha", "Senior Product Manager"),
        ("Beta", "Product Manager"), ("Gamma", "Product Manager")]
    held = ranking.held_back(conn, ranking.latest_shortlist(conn)["id"])
    assert [h["title"] for h in held] == ["Senior Product Manager, Growth", "Product Manager"]

    assert cli.main(["shortlist", "--by-company"]) == 0
    out = capsys.readouterr().out
    assert "2 held back by the per-company cap" in out.splitlines()[0]
    assert "Alpha: 2 roles, best rank 1; 2 more held back by the per-company cap" in out
    assert "Beta: 1 role, best rank 3\n" in out

    assert cli.main(["why", str(role_id(conn, "a3"))]) == 0
    out = capsys.readouterr().out
    assert ("Not on the shortlist: held back by the cap of 2 roles per company: Alpha already has "
            "that many higher-ranked roles on the list. It scores 40, enough to be listed otherwise.") in out
    info = ranking.explain(conn, role_id(conn, "a3"), ranking.load_config(config))
    assert info["held_back_by_cap"] is True and info["rank"] is None
    assert ranking.explain(conn, role_id(conn, "a1"), ranking.load_config(config))["held_back_by_cap"] is False

    # A saved role is never held back, and it uses one of the company's slots.
    assert cli.main(["mark", str(role_id(conn, "a4")), "saved"]) == 0
    capsys.readouterr()
    alpha = [e["title"] for e in ranking.shortlist_entries(conn, ranking.latest_shortlist(conn)["id"])
             if e["company"] == "Alpha"]
    assert alpha == ["Staff Product Manager", "Senior Product Manager", "Product Manager"]

    # 0 turns the cap off.
    config.write_text(CAPPED.replace("max_per_company = 2", "max_per_company = 0"))
    assert cli.main(["shortlist", "rebuild"]) == 0
    assert "held back" not in capsys.readouterr().out
    assert [e["company"] for e in ranking.shortlist_entries(
        conn, ranking.latest_shortlist(conn)["id"])] == ["Alpha"] * 4


def test_cap_defaults_to_five_and_is_validated():
    assert cfg().max_per_company == 5                      # a config that predates the setting
    assert cfg(ranking.starter_config()).max_per_company == 5
    assert "max_per_company = 5" in ranking.starter_config()
    with pytest.raises(ranking.ConfigError, match="max_per_company"):
        cfg("max_per_company = -1")


def test_excluded_title_phrase_never_makes_the_list(conn, world):
    world("Alpha", [mk("a1", "Product Manager Intern"), mk("a2", "Product Manager")], on_day=10)
    ranking.build_shortlist(conn, cfg(), now=day(10))
    assert [t for _, t, _ in listed(conn)] == ["Product Manager"]
    info = ranking.explain(conn, role_id(conn, "a1"), cfg(), now=day(10))
    assert info["rank"] is None
    assert info["why_not"] == 'its title contains "Intern", a phrase you never want'


def test_deactivated_companies_and_closed_roles_are_never_shortlisted(conn, world):
    world("Alpha", [mk("a1", "Product Manager")], on_day=10)
    world("Beta", [mk("b1", "Product Manager"), mk("b2", "Senior Product Manager")], on_day=10)
    world("Beta", [mk("b1", "Product Manager")], on_day=11)
    world("Beta", [mk("b1", "Product Manager")], on_day=12)          # b2 closes
    companies.set_active(conn, "Alpha", False)
    ranking.build_shortlist(conn, cfg(), now=day(12))
    assert [t for _, t, _ in listed(conn)] == ["Product Manager"]      # Beta's b1 only
    assert ranking.explain(conn, role_id(conn, "a1"), cfg(), now=day(12))["why_not"] == "Alpha is deactivated"
    assert ranking.explain(conn, role_id(conn, "b2"), cfg(), now=day(12))["why_not"] == "the role closed on 2026-03-12"


# --- marks -------------------------------------------------------------------

def test_dismissed_and_applied_drop_off_and_saved_stays_flagged(conn, world, capsys, isolated_db_path):
    world("Alpha", [mk("a1", "Senior Product Manager"), mk("a2", "Product Manager"),
                    mk("a3", "Staff Product Manager")], on_day=10)
    world("Beta", [mk("b1", "Product Manager"), mk("b2", "Product Manager", "New York")], on_day=10)
    config = isolated_db_path.parent / "ranking.toml"
    config.write_text(SMALL_CONFIG)
    assert cli.main(["shortlist", "rebuild"]) == 0
    assert [t for _, t, _ in listed(conn)] == [
        "Staff Product Manager", "Senior Product Manager", "Product Manager"]
    staff, senior, pm = (role_id(conn, j) for j in ("a3", "a1", "a2"))

    # Dismissed: gone at once, and the next role moves up.
    assert cli.main(["mark", str(staff), "dismissed", "--note", "wrong team"]) == 0
    assert "it will not be shortlisted again" in capsys.readouterr().out
    assert [t for _, t, _ in listed(conn)] == ["Senior Product Manager", "Product Manager", "Product Manager"]
    assert ranking.explain(conn, staff, ranking.load_config(config))["why_not"] == "you marked it dismissed"

    # Applied: also gone. Saved: stays, flagged.
    assert cli.main(["mark", f"#{senior}", "applied"]) == 0
    assert cli.main(["mark", str(pm), "saved", "--note", "ask Dana for a referral"]) == 0
    capsys.readouterr()
    assert [e for e in listed(conn)][0][1] == "Product Manager"
    assert cli.main(["shortlist"]) == 0
    out = capsys.readouterr().out
    assert "saved" in out and "Senior Product Manager" not in out and "Staff Product Manager" not in out

    # Still gone after later rebuilds, on later days.
    ranking.build_shortlist(conn, ranking.load_config(config), now=day(11))
    assert {staff, senior}.isdisjoint(
        e["id"] for e in ranking.shortlist_entries(conn, ranking.latest_shortlist(conn)["id"]))

    assert cli.main(["marks"]) == 0
    out = capsys.readouterr().out
    assert "wrong team" in out and "ask Dana for a referral" in out and "3 marked roles." in out

    # Undo: the role is eligible again.
    assert cli.main(["unmark", str(staff)]) == 0
    assert f"Removed the dismissed mark from Alpha, Staff Product Manager (ID {staff})." in capsys.readouterr().out
    assert staff in {e["id"] for e in ranking.shortlist_entries(conn, ranking.latest_shortlist(conn)["id"])}
    assert cli.main(["unmark", str(staff)]) == 0
    assert "had no mark" in capsys.readouterr().out
    assert cli.main(["mark", "99999", "saved"]) == 1
    assert cli.main(["mark", "abc", "saved"]) == 1
    assert "is not a role ID" in capsys.readouterr().err


def descriptions(conn):
    return {r[0] for r in conn.execute(
        "SELECT r.source_job_id FROM role_descriptions d JOIN roles r ON r.id = d.role_id")}


def test_saved_and_applied_roles_keep_their_description_after_closing(conn, world):
    world("Alpha", [mk(j, "Product Manager") for j in ("saved", "applied", "dismissed", "plain", "stays")],
          on_day=10)
    for job in ("saved", "applied", "dismissed"):
        ranking.mark_role(conn, role_id(conn, job), job)
    # Missing from two successful fetches: all four close.
    world("Alpha", [mk("stays", "Product Manager")], on_day=11)
    world("Alpha", [mk("stays", "Product Manager")], on_day=12)
    closed = {r[0] for r in conn.execute("SELECT source_job_id FROM roles WHERE closed_at IS NOT NULL")}
    assert closed == {"saved", "applied", "dismissed", "plain"}
    assert descriptions(conn) == {"saved", "applied", "stays"}       # the other two lost theirs, as before
    assert conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0] == 5

    # Dropping the mark on a closed role lets its description go, like any other closed role.
    ranking.unmark_role(conn, role_id(conn, "saved"))
    assert descriptions(conn) == {"applied", "stays"}
    ranking.mark_role(conn, role_id(conn, "applied"), "dismissed")
    assert descriptions(conn) == {"stays"}


def test_unmapping_a_board_also_keeps_saved_descriptions(conn, world):
    world("Alpha", [mk("keep", "Product Manager"), mk("drop", "Product Manager")], on_day=10)
    ranking.mark_role(conn, role_id(conn, "keep"), "saved")
    assert companies.remove_board(conn, "Alpha", "greenhouse", "alpha") == 2
    assert descriptions(conn) == {"keep"}


# --- history -----------------------------------------------------------------

def test_new_since_yesterday(conn, world, capsys, isolated_db_path):
    (isolated_db_path.parent / "ranking.toml").write_text(SMALL_CONFIG.replace("size = 3", "size = 10"))
    config = ranking.load_config(isolated_db_path.parent / "ranking.toml")
    world("Alpha", [mk("a1", "Product Manager"), mk("a2", "Senior Product Manager")], on_day=10)
    first = ranking.build_shortlist(conn, config, now=day(10))
    assert (first["new"], first["previous_date"]) == (None, None)

    world("Alpha", [mk("a1", "Product Manager"), mk("a2", "Senior Product Manager"),
                    mk("a3", "Staff Product Manager")], on_day=11)
    world("Beta", [mk("b1", "Group Product Manager")], on_day=11)
    second = ranking.build_shortlist(conn, config, now=day(11))
    assert (second["new"], second["previous_date"]) == (2, "2026-03-10")

    # A rebuild later the same day replaces that day's list: "yesterday" stays yesterday.
    again = ranking.build_shortlist(conn, config, now=day(11, hour=18))
    assert (again["new"], again["previous_date"]) == (2, "2026-03-10")
    assert conn.execute("SELECT COUNT(*) FROM shortlists").fetchone()[0] == 2

    assert cli.main(["shortlist", "--new"]) == 0
    out = capsys.readouterr().out
    assert "new since 2026-03-10: 2" in out
    assert "Staff Product Manager" in out and "Group Product Manager" in out
    assert "Senior Product Manager" not in out

    world("Beta", [mk("b1", "Group Product Manager"), mk("b2", "Product Manager")], on_day=12)
    ranking.build_shortlist(conn, config, now=day(12))
    previous, new_ids = ranking.new_since(conn, ranking.latest_shortlist(conn)["id"])
    assert (previous["list_date"], new_ids) == ("2026-03-11", {role_id(conn, "b2")})
    # --since reaches further back.
    previous, new_ids = ranking.new_since(conn, ranking.latest_shortlist(conn)["id"], since="2026-03-10")
    assert previous["list_date"] == "2026-03-10" and len(new_ids) == 3
    assert cli.main(["shortlist", "--new", "--since", "2026-03-10", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["compared_with"] == "2026-03-10" and len(payload["entries"]) == 3
    assert all(e["new"] for e in payload["entries"])
    assert cli.main(["shortlist", "--since", "soon"]) == 1


def test_lists_are_kept_for_thirty_days(conn, world):
    world("Alpha", [mk("a1", "Product Manager")], on_day=1)
    for n in (1, 2, 20, 31):
        ranking.build_shortlist(conn, cfg(), now=day(n))
    april_first = datetime(2026, 4, 1, 12, tzinfo=timezone.utc)   # 31 days after March 1, 30 after March 2
    ranking.build_shortlist(conn, cfg(), now=april_first)
    dates = [r[0] for r in conn.execute("SELECT list_date FROM shortlists ORDER BY list_date")]
    assert dates == ["2026-03-02", "2026-03-20", "2026-03-31", "2026-04-01"]
    assert conn.execute("SELECT COUNT(*) FROM shortlist_entries").fetchone()[0] == 4  # entries pruned with them


# --- config changes need no network ------------------------------------------

def test_config_change_reorders_the_list_without_a_fetch(conn, world, capsys, isolated_db_path):
    world("Alpha", [mk("a1", "Product Manager", "Austin")], on_day=10)
    world("Beta", [mk("b1", "Senior Product Manager", "New York")], on_day=10)
    config = isolated_db_path.parent / "ranking.toml"
    config.write_text(SMALL_CONFIG)
    assert cli.main(["shortlist", "rebuild"]) == 0
    assert "Shortlist rebuilt: 2 roles (of 2 that qualify)" in capsys.readouterr().out
    # Recency is 0 here (the CLI ranks as of today, long after these roles were first seen).
    assert [(t, s) for _, t, s in listed(conn)] == [("Senior Product Manager", 50.0), ("Product Manager", 30.0)]
    runs_before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    # Make Austin a preferred location worth far more: Alpha's plain PM role now leads.
    config.write_text(SMALL_CONFIG.replace('"New York" = 10', '"New York" = 10\n"Austin" = 100'))
    assert cli.main(["shortlist"]) == 0
    assert "The ranking config has changed since this list was built" in capsys.readouterr().out
    assert cli.main(["shortlist", "rebuild"]) == 0     # the socket block in conftest proves no network
    capsys.readouterr()
    assert [(t, s) for _, t, s in listed(conn)] == [("Product Manager", 130.0), ("Senior Product Manager", 50.0)]
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs_before          # no scout ran
    assert cli.main(["shortlist"]) == 0
    assert "has changed since" not in capsys.readouterr().out


def test_db_and_config_flags_work_before_or_after_the_subcommand():
    parser = cli.build_parser()
    for argv in (["shortlist", "--db", "X", "--config", "C", "rebuild"],
                 ["shortlist", "rebuild", "--db", "X", "--config", "C"]):
        args = parser.parse_args(argv)
        assert (args.db, args.config, args.func) == ("X", "C", cli._cmd_shortlist_rebuild)
    assert parser.parse_args(["shortlist", "init"]).db is None


def test_bad_configs_are_refused_with_a_clear_message(conn, capsys, isolated_db_path):
    config = isolated_db_path.parent / "ranking.toml"
    assert cli.main(["shortlist", "rebuild"]) == 1
    assert "Create one with: rolescout shortlist init" in capsys.readouterr().err
    for text, message in (
            ("size = ", "is not valid TOML"),
            ("[titel.wanted]\n", "unknown setting titel"),
            ('[title.wanted]\n"PM" = "lots"\n', "must have a number of points"),
            ("size = 0\n", "size must be a whole number of 1 or more"),
            ("[recency]\nfull_days = 5\nzero_days = 5\n", "zero_days must be larger")):
        config.write_text(text)
        assert cli.main(["shortlist", "rebuild"]) == 1
        assert message in capsys.readouterr().err


# --- the scout rebuilds the list ---------------------------------------------

def scripted_scout(monkeypatch, script):
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: scout.run_scout(
        c, adapter_for=script, on_board=kw.get("on_board")))


def test_shortlist_is_rebuilt_at_the_end_of_every_scout(conn, monkeypatch, capsys, isolated_db_path):
    companies.add_company(conn, "Alpha")
    companies.add_board(conn, "Alpha", "greenhouse", "alpha")
    scripted_scout(monkeypatch, Scripted(alpha=[
        [mk("a1", "Product Manager")],
        [mk("a1", "Product Manager"), mk("a2", "Senior Product Manager")],
        [mk("a1", "Product Manager"), mk("a2", "Senior Product Manager")]]))

    # No config yet: the scout succeeds and says how to get a shortlist.
    assert cli.main(["scout"]) == 0
    out = capsys.readouterr().out
    assert "No shortlist: there is no ranking config at" in out and "rolescout shortlist init" in out
    assert ranking.latest_shortlist(conn) is None

    (isolated_db_path.parent / "ranking.toml").write_text(SMALL_CONFIG)
    assert cli.main(["scout"]) == 0
    assert "Shortlist rebuilt: 2 roles (of 2 that qualify). See: rolescout shortlist" in capsys.readouterr().out
    current = ranking.latest_shortlist(conn)
    assert current["run_id"] == 2 and len(ranking.shortlist_entries(conn, current["id"])) == 2

    # A broken config never fails the scout; the previous list is kept.
    (isolated_db_path.parent / "ranking.toml").write_text("size = ")
    assert cli.main(["scout"]) == 0
    assert "Shortlist not rebuilt, the previous one is kept:" in capsys.readouterr().out
    assert ranking.latest_shortlist(conn)["id"] == current["id"]


def test_daily_job_rebuilds_too(conn, monkeypatch, capsys, isolated_db_path):
    companies.add_company(conn, "Alpha")
    companies.add_board(conn, "Alpha", "greenhouse", "alpha")
    scripted_scout(monkeypatch, Scripted(alpha=[[mk("a1", "Product Manager")]]))
    (isolated_db_path.parent / "ranking.toml").write_text(SMALL_CONFIG)
    assert cli.main(["daily", "--recheck-days", "0"]) == 0
    assert "Shortlist rebuilt: 1 roles" in capsys.readouterr().out


# --- starter config and CLI views --------------------------------------------

FILTERS_CSV = REPO / "tests" / "fixtures" / "role_filters_csv_with_titles_seniority_and_domains.csv"


def test_init_seeds_from_a_filters_file_when_asked_and_never_overwrites(conn, capsys, isolated_db_path):
    path = isolated_db_path.parent / "ranking.toml"
    assert cli.main(["shortlist", "init", "--filters", str(FILTERS_CSV)]) == 0
    out = capsys.readouterr().out
    assert f"Wrote {path}: 5 title phrases and 3 seniority words from {FILTERS_CSV}." in out
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = ranking.load_config(path)
    assert loaded.size == 100 and "tier_points" not in path.read_text()
    # A config written before tiers stopped earning points still loads.
    assert ranking.parse_config(path.read_text() + "\n[tier_points]\n1 = 30\n").size == 100
    assert list(loaded.wanted)[:3] == ["Head of Product", "Staff Product Manager", "Senior Product Manager"]
    assert set(loaded.seniority) == {"Staff", "Senior", "Head"}
    assert loaded.exclude == ranking.STARTER_EXCLUDE and loaded.preferred_locations == {}
    assert (loaded.remote_points, loaded.recency_max_points) == (0.0, 15.0)

    path.write_text(path.read_text() + "\n# my edit\n")
    assert cli.main(["shortlist", "init"]) == 1
    assert "already exists" in capsys.readouterr().err and "# my edit" in path.read_text()
    assert cli.main(["shortlist", "init", "--force"]) == 0
    assert "# my edit" not in path.read_text()


def test_init_writes_a_generic_starter_for_product_and_engineering(conn, capsys, tmp_path, monkeypatch):
    (tmp_path / "role_filters.csv").write_text(FILTERS_CSV.read_text())
    monkeypatch.chdir(tmp_path)                  # a filters file in the current folder is not picked up
    target = tmp_path / "elsewhere" / "rank.toml"
    assert cli.main(["shortlist", "init", "--config", str(target)]) == 0
    assert ("the starter ranking for product and engineering roles (27 title phrases, no location preference)"
            in capsys.readouterr().out)
    loaded, text = ranking.load_config(target), target.read_text()
    assert list(loaded.wanted) == ranking.STARTER_TITLES
    assert {"Product Manager", "Software Engineer", "Engineering Manager"} <= set(loaded.wanted)
    assert set(loaded.wanted.values()) == {30.0} and set(loaded.seniority.values()) == {10.0}   # no title favored
    assert {"Intern", "New Grad"} <= set(loaded.exclude)
    # No location preference and no leveling until the owner adds one; each has a commented example.
    assert (loaded.remote_points, loaded.preferred_locations, loaded.required_locations) == (0.0, {}, [])
    assert loaded.senior_as_staff == []
    for example in ('# require = ["United States"]', '#   "New York" = 10', "Example: remote_points = 10",
                    'senior_as_staff = ["Acme", "Globex"]'):
        assert example in text
    for wanted, title in ((True, "Senior Product Manager, Growth"), (True, "Staff Software Engineer"),
                          (False, "Product Manager Intern"), (False, "Software Engineer, New Grad"),
                          (False, "Product Marketing Manager")):
        assert loaded.wants_title(title) is wanted, title
    assert cli.main(["shortlist", "init", "--force", "--filters", str(tmp_path / "nope.csv")]) == 1


def test_views_by_company_top_and_why(conn, world, capsys, isolated_db_path):
    (isolated_db_path.parent / "ranking.toml").write_text(SMALL_CONFIG.replace("size = 3", "size = 10"))
    world("Beta", [mk("b1", "Group Product Manager", "Remote"), mk("b2", "Product Manager")], on_day=10)
    world("Alpha", [mk("a1", "Senior Product Manager", "New York"), mk("a2", "Product Manager")], on_day=10)
    world("Gamma", [mk("g1", "Product Manager")], on_day=10)
    assert cli.main(["shortlist"]) == 0
    assert "No shortlist yet. Run: rolescout shortlist rebuild" in capsys.readouterr().out
    assert cli.main(["shortlist", "rebuild"]) == 0
    capsys.readouterr()

    assert cli.main(["shortlist", "--top", "2"]) == 0
    out = capsys.readouterr().out
    assert "5 roles of 5 that qualify; no earlier list to compare with" in out
    rows = [line for line in out.splitlines() if line[:1].isdigit()]
    assert len(rows) == 2 and "Senior Product Manager" in rows[0] and str(role_id(conn, "a1")) in rows[0]

    assert cli.main(["shortlist", "--by-company"]) == 0
    out = capsys.readouterr().out
    headers = [line for line in out.splitlines() if "best rank" in line]
    # Companies in order of their best-ranked role.
    assert headers == ["Alpha: 2 roles, best rank 1", "Beta: 2 roles, best rank 2", "Gamma: 1 role, best rank 5"]

    assert cli.main(["shortlist", "--new"]) == 0
    assert "there is no earlier list, so nothing can be new yet" in capsys.readouterr().out

    top = role_id(conn, "a1")
    assert cli.main(["why", str(top)]) == 0
    out = capsys.readouterr().out
    assert f"Senior Product Manager  (ID {top})" in out and "Alpha | New York" in out and "tier" not in out.lower()
    assert "Rank 1 of 5 on the shortlist for" in out
    for line in ('Title matches "Product Manager" (+30)', 'Seniority: "Senior" (+10)',
                 'Location matches "New York" (+10)'):
        assert line in out
    gamma = role_id(conn, "g1")
    assert cli.main(["why", str(gamma), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["rank"] == 5 and not any("ier" in line for line in info["detail"])
    assert "Not remote and not in a preferred location (+0)" in info["detail"]


def test_why_explains_a_role_below_the_cut(conn, world):
    world("Alpha", [mk(f"a{i}", "Senior Product Manager") for i in range(3)] + [mk("low", "Product Manager")],
          on_day=10)
    ranking.build_shortlist(conn, cfg(), now=day(10))          # size = 3
    info = ranking.explain(conn, role_id(conn, "low"), cfg(), now=day(10))
    assert info["rank"] is None
    assert info["why_not"] == "3 roles rank above it: it scores 40 and the last role on the list scores 50"


# --- upgrade -----------------------------------------------------------------

def test_version_2_database_upgrades_in_place(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "SCHEMA_VERSION", 2)
    old = db.connect(path)
    assert db.schema_version(old) == 2
    old.execute("INSERT INTO companies (name, tier, created_at) VALUES ('Alpha', 1, 'x')")
    old.execute("INSERT INTO boards (company_id, source, ref, mapped_at) VALUES (1, 'greenhouse', 'alpha', 'x')")
    old.execute("INSERT INTO roles (source, source_job_id, company_id, board_id, title, first_seen_at, last_seen_at) "
                "VALUES ('greenhouse', 'a1', 1, 1, 'Product Manager', '2026-03-10T08:00:00Z', '2026-03-10T08:00:00Z')")
    old.close()
    monkeypatch.undo()

    conn = db.connect(path)
    assert db.schema_version(conn) == db.SCHEMA_VERSION >= 3
    ranking.build_shortlist(conn, cfg(), now=day(10))
    assert listed(conn) == [(1, "Product Manager", 40.0)]
    ranking.mark_role(conn, 1, "saved", "from the old database")
    assert ranking.list_marks(conn)[0]["note"] == "from the old database"
