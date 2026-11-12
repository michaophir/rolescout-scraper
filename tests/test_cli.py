import json

import pytest

from rolescout import __version__, cli, scout
from rolescout.adapters import AshbyAdapter, BoardNotFound
from rolescout.http import USER_AGENT, make_session


def test_user_agent_names_project_and_repo():
    assert USER_AGENT == (
        f"rolescout/{__version__} (+https://github.com/michaophir/rolescout-scraper)")
    assert make_session().headers["User-Agent"] == USER_AGENT


def test_fetch_prints_count_and_sample(monkeypatch, capsys, load_fixture, fake_session):
    session = fake_session(load_fixture("ashby_ramp.json"))
    monkeypatch.setattr(cli, "get_adapter", lambda source: AshbyAdapter(session))

    assert cli.main(["fetch", "ashby", "ramp", "--sample", "2"]) == 0
    out = capsys.readouterr().out
    assert "ashby/ramp: 3 open roles" in out
    assert "Sample role (2 of 3)" in out
    assert "Software Engineer, Android" in out
    assert "--full to show all" in out  # description is previewed, not dumped


def test_fetch_reports_missing_board(monkeypatch, capsys):
    class Missing:
        def fetch(self, slug):
            raise BoardNotFound(f"ashby: no board found for {slug!r}")
    monkeypatch.setattr(cli, "get_adapter", lambda source: Missing())

    assert cli.main(["fetch", "ashby", "nope"]) == 1
    assert "no board found" in capsys.readouterr().err


def test_unknown_source_is_rejected():
    with pytest.raises(SystemExit):
        cli.main(["fetch", "workday", "acme"])


# --- database-backed commands ------------------------------------------------

def run(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_company_commands_end_to_end(capsys, tmp_path):
    csv_path = tmp_path / "c.csv"
    csv_path.write_text("company_name,website,tier\nAcme,https://acme.com,1\nBolt,https://bolt.com,2\n")
    assert run(capsys, "companies", "import", str(csv_path))[1] == f"Imported {csv_path}: 2 added, 0 updated.\n"
    assert run(capsys, "companies", "add", "Cobalt", "--tier", "3", "--tags", "ai,b2b")[0] == 0
    assert run(capsys, "companies", "map", "acme", "greenhouse", "acme")[1] == "Acme -> greenhouse/acme\n"
    assert run(capsys, "companies", "deactivate", "Bolt")[1] == "Bolt is now inactive.\n"

    code, out, _ = run(capsys, "companies", "list")
    assert code == 0
    assert "greenhouse/acme" in out and "ai, b2b" in out
    assert "3 companies, 1 mapped to a board." in out
    code, out, _ = run(capsys, "companies", "list", "--active", "--json")
    assert [c["name"] for c in json.loads(out)] == ["Acme", "Cobalt"]

    code, _, err = run(capsys, "companies", "deactivate", "Nope")
    assert (code, err) == (1, "error: no company named 'Nope'\n")
    code, _, err = run(capsys, "companies", "add", "acme")
    assert code == 1 and "already exists" in err


def test_db_flag_overrides_environment(capsys, tmp_path, isolated_db_path):
    other = tmp_path / "other.db"
    assert run(capsys, "companies", "add", "Acme", "--db", str(other))[0] == 0
    assert other.exists() and not isolated_db_path.exists()


def test_scout_and_query_commands(monkeypatch, capsys):
    from tests.test_scout import Scripted, role
    script = Scripted(acme=[[role("A1", title="Product Manager"), role("A2", title="Engineer")]],
                      bolt=[BoardNotFound("lever: no board found for 'bolt'")])

    def scripted_scout(conn, **kwargs):
        return scout.run_scout(conn, adapter_for=script, on_company=kwargs.get("on_company"))
    monkeypatch.setattr(cli, "run_scout", scripted_scout)

    run(capsys, "companies", "add", "Acme")
    run(capsys, "companies", "add", "Bolt")
    run(capsys, "companies", "add", "Unmapped")
    run(capsys, "companies", "map", "Acme", "greenhouse", "acme")
    run(capsys, "companies", "map", "Bolt", "lever", "bolt")

    code, out, _ = run(capsys, "scout")
    assert code == 0  # a partial failure is recorded, not fatal
    assert "Run 1 completed" in out
    assert "FAILED: lever: no board found for 'bolt'" in out
    assert "1 ok, 1 failed" in out
    assert "Skipped 1 active companies with no board mapping." in out

    code, out, _ = run(capsys, "query", "--title", "product manager")
    assert code == 0
    assert "Product Manager" in out and "Engineer" not in out
    assert "https://example.com/A1" in out and out.endswith("\n1 role.\n")

    code, out, _ = run(capsys, "query", "--company", "acme", "--json")
    assert [r["title"] for r in json.loads(out)] == ["Engineer", "Product Manager"]
    assert run(capsys, "query", "--closed")[1].endswith("\n0 roles.\n")
    assert run(capsys, "query", "--company", "Nope")[0] == 1
    assert run(capsys, "query", "--since", "yesterday")[0] == 1


def test_scout_exits_nonzero_when_every_company_fails(monkeypatch, capsys):
    from tests.test_scout import Scripted
    script = Scripted(acme=[BoardNotFound("nope")])
    monkeypatch.setattr(cli, "run_scout", lambda conn, **kw: scout.run_scout(conn, adapter_for=script))
    run(capsys, "companies", "add", "Acme")
    run(capsys, "companies", "map", "Acme", "greenhouse", "acme")
    assert run(capsys, "scout")[0] == 1


def test_polite_session_spaces_out_requests(monkeypatch):
    import requests
    from rolescout import http

    now = [100.0]
    sleeps = []
    monkeypatch.setattr(http.time, "monotonic", lambda: now[0])

    def fake_sleep(seconds):
        sleeps.append(round(seconds, 3))
        now[0] += seconds
    monkeypatch.setattr(http.time, "sleep", fake_sleep)

    def fake_request(self, *args, **kwargs):
        now[0] += 0.25  # each request takes a quarter second
        return "response"
    monkeypatch.setattr(requests.Session, "request", fake_request)

    session = http.make_session(min_interval=1.0)
    assert session.headers["User-Agent"] == http.USER_AGENT
    for _ in range(3):
        assert session.get("https://example.com") == "response"
    assert sleeps == [1.0, 1.0]  # none before the first; a full second after each response

    now[0] += 5  # already waited long enough: no extra sleep
    session.get("https://example.com")
    assert sleeps == [1.0, 1.0]
