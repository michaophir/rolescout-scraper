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


def mk(job_id, title, location="Austin", workplace="", source="greenhouse"):
    return Role(source=source, source_job_id=job_id, company="x", title=title, department="Product",
                location=location, workplace_type=workplace, url=f"https://example.com/{job_id}",
                date_posted="2026-03-01", compensation_text="", description=f"About {job_id}")


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
        company_id = companies.add_company(conn, name, tier=tier)
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
        mk("a1", "Senior Product Manager, Growth", "New York, NY"),   # 30 tier + 30 title + 10 senior + 10 NY + 10 new
        mk("a2", "Product Manager"),                                  # 30 + 30 + 10 new
        mk("a3", "Staff Software Engineer", "New York"),              # no wanted title: never listed
    ], on_day=10)
    world("Beta", [
        mk("b1", "Group Product Manager", "Remote - US"),             # 20 + 40 (best phrase) + 5 remote + 10 new
        mk("b2", "Product Manager", workplace="remote"),              # 20 + 30 + 5 + 10
    ], on_day=10)
    world("Gamma", [mk("g1", "Staff Product Manager")], on_day=10)   # no tier: 0 + 30 + 15 + 10

    result = ranking.build_shortlist(conn, cfg(), now=day(10))
    assert (result["listed"], result["eligible"], result["new"]) == (3, 5, None)
    assert listed(conn) == [
        (1, "Senior Product Manager, Growth", 90.0),
        (2, "Group Product Manager", 75.0),
        (3, "Product Manager", 70.0),              # Alpha's: size = 3 cuts the rest
    ]
    (top,) = [e for e in ranking.shortlist_entries(conn, result["shortlist_id"]) if e["rank"] == 1]
    assert top["reasons"] == [
        "Tier 1 company (+30)", 'Title matches "Product Manager" (+30)',
        'Seniority: "Senior" (+10)', 'Location matches "New York" (+10)', "First seen today (+10)"]
    assert (top["company"], top["tier"], top["id"]) == ("Alpha", 1, role_id(conn, "a1"))


def test_phrases_match_whole_words_only():
    c = cfg()
    base = {"location": "", "workplace_type": "", "first_seen_at": stamp(10), "tier": 1}
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
    role = {"title": "Product Manager", "location": "", "workplace_type": "", "tier": None}
    def points(first_seen_day, today):
        return ranking.evaluate({**role, "first_seen_at": stamp(first_seen_day)}, c, day(today).date()).score - 30
    assert [points(1, 1), points(1, 2), points(1, 7), points(1, 12), points(1, 30)] == [10, 10, 5, 0, 0]


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
    assert [(t, s) for _, t, s in listed(conn)] == [("Senior Product Manager", 70.0), ("Product Manager", 60.0)]
    runs_before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

    # Make tier 1 worth far more: Alpha's plain PM role now leads.
    config.write_text(SMALL_CONFIG.replace("1 = 30", "1 = 100"))
    assert cli.main(["shortlist"]) == 0
    assert "The ranking config has changed since this list was built" in capsys.readouterr().out
    assert cli.main(["shortlist", "rebuild"]) == 0     # the socket block in conftest proves no network
    capsys.readouterr()
    assert [(t, s) for _, t, s in listed(conn)] == [("Product Manager", 130.0), ("Senior Product Manager", 70.0)]
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
            ("[recency]\nfull_days = 5\nzero_days = 5\n", "zero_days must be larger"),
            ('[tier_points]\ngold = 5\n', "is not a tier number")):
        config.write_text(text)
        assert cli.main(["shortlist", "rebuild"]) == 1
        assert message in capsys.readouterr().err


# --- the scout rebuilds the list ---------------------------------------------

def scripted_scout(monkeypatch, script):
    monkeypatch.setattr(cli, "run_scout", lambda c, **kw: scout.run_scout(
        c, adapter_for=script, on_board=kw.get("on_board")))


def test_shortlist_is_rebuilt_at_the_end_of_every_scout(conn, monkeypatch, capsys, isolated_db_path):
    companies.add_company(conn, "Alpha", tier=1)
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
    companies.add_company(conn, "Alpha", tier=1)
    companies.add_board(conn, "Alpha", "greenhouse", "alpha")
    scripted_scout(monkeypatch, Scripted(alpha=[[mk("a1", "Product Manager")]]))
    (isolated_db_path.parent / "ranking.toml").write_text(SMALL_CONFIG)
    assert cli.main(["daily", "--recheck-days", "0"]) == 0
    assert "Shortlist rebuilt: 1 roles" in capsys.readouterr().out


# --- starter config and CLI views --------------------------------------------

def test_init_seeds_from_the_repo_filters_and_never_overwrites(conn, capsys, isolated_db_path, monkeypatch):
    monkeypatch.chdir(REPO)
    path = isolated_db_path.parent / "ranking.toml"
    assert cli.main(["shortlist", "init"]) == 0
    out = capsys.readouterr().out
    assert f"Wrote {path}: 21 title phrases and 7 seniority words from role_filters.csv." in out
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = ranking.load_config(path)
    assert loaded.size == 100 and loaded.tier_points == {1: 30.0, 2: 20.0, 3: 10.0}
    assert list(loaded.wanted)[:3] == ["Chief Product Officer", "CPO", "Vice President of Product"]
    assert set(loaded.seniority) == {"Principal", "Director", "VP", "Head", "Staff", "Senior", "Founding"}
    assert loaded.exclude == [] and loaded.preferred_locations == {}
    assert (loaded.remote_points, loaded.recency_max_points) == (10.0, 15.0)

    path.write_text(path.read_text() + "\n# my edit\n")
    assert cli.main(["shortlist", "init"]) == 1
    assert "already exists" in capsys.readouterr().err and "# my edit" in path.read_text()
    assert cli.main(["shortlist", "init", "--force"]) == 0
    assert "# my edit" not in path.read_text()


def test_init_without_a_filters_file_writes_marked_examples(conn, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "elsewhere" / "rank.toml"
    assert cli.main(["shortlist", "init", "--config", str(target)]) == 0
    assert "with example titles only" in capsys.readouterr().out
    assert "EXAMPLES ONLY" in target.read_text()
    assert list(ranking.load_config(target).wanted) == ["Senior Product Manager", "Product Manager"]
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
    assert headers == ["Alpha (tier 1): 2 roles, best rank 1", "Beta (tier 2): 2 roles, best rank 2",
                       "Gamma (no tier): 1 role, best rank 5"]

    assert cli.main(["shortlist", "--new"]) == 0
    assert "there is no earlier list, so nothing can be new yet" in capsys.readouterr().out

    top = role_id(conn, "a1")
    assert cli.main(["why", str(top)]) == 0
    out = capsys.readouterr().out
    assert f"Senior Product Manager  (ID {top})" in out and "Alpha (tier 1) | New York" in out
    assert "Rank 1 of 5 on the shortlist for" in out
    for line in ("Tier 1 company (+30)", 'Title matches "Product Manager" (+30)', 'Seniority: "Senior" (+10)',
                 'Location matches "New York" (+10)'):
        assert line in out
    gamma = role_id(conn, "g1")
    assert cli.main(["why", str(gamma), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["rank"] == 5 and "Company has no tier (+0)" in info["detail"]
    assert "Not remote and not in a preferred location (+0)" in info["detail"]


def test_why_explains_a_role_below_the_cut(conn, world):
    world("Alpha", [mk(f"a{i}", "Senior Product Manager") for i in range(3)] + [mk("low", "Product Manager")],
          on_day=10)
    ranking.build_shortlist(conn, cfg(), now=day(10))          # size = 3
    info = ranking.explain(conn, role_id(conn, "low"), cfg(), now=day(10))
    assert info["rank"] is None
    assert info["why_not"] == "3 roles rank above it: it scores 70 and the last role on the list scores 80"


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
    assert listed(conn) == [(1, "Product Manager", 70.0)]
    ranking.mark_role(conn, 1, "saved", "from the old database")
    assert ranking.list_marks(conn)[0]["note"] == "from the old database"
