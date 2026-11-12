import pytest

from rolescout import __version__, cli
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
