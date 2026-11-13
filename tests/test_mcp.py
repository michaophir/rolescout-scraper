"""The MCP server: every tool called in process against a temporary database."""

import asyncio
import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("mcp", reason='needs the optional extra: pip install "rolescout[mcp]"')

from mcp import Client  # noqa: E402

from rolescout import cli, companies, mcp_server, resolve, scout  # noqa: E402
from rolescout import shortlist as ranking  # noqa: E402
from rolescout.adapters import SourceError  # noqa: E402
from tests.test_scout import Clock, Scripted  # noqa: E402
from tests.test_shortlist import SMALL_CONFIG, mk  # noqa: E402

READ_TOOLS = {"get_shortlist", "get_role", "search_roles", "explain_rank", "list_marks",
              "list_target_companies", "coverage_gaps", "connector_brief", "hiring_trends", "health"}
WRITE_TOOLS = {"mark_role", "unmark_role", "add_target_company", "deactivate_company",
               "reactivate_company", "map_board", "record_job_system"}


class Session:
    """Calls tools on a server through a real MCP client, in memory."""

    def __init__(self, server):
        self.server = server

    def _run(self, work):
        async def main():
            async with Client(self.server) as client:
                return await work(client)
        return asyncio.run(main())

    def tools(self):
        return self._run(lambda c: c.list_tools()).tools

    def call(self, tool, /, **arguments):
        result = self._run(lambda c: c.call_tool(tool, arguments))
        assert not result.is_error, result.content[0].text
        return result.structured_content

    def error(self, tool, /, **arguments):
        result = self._run(lambda c: c.call_tool(tool, arguments))
        assert result.is_error
        return result.content[0].text


@pytest.fixture
def world(conn, isolated_db_path, monkeypatch, tmp_path):
    """Three scout runs over three companies, a ranking config and a shortlist per day."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no real launchd schedule is read
    for name, tier, ref in (("Alpha", 1, "alpha"), ("Beta", 2, "beta"), ("Gamma", 3, "gamma")):
        companies.add_company(conn, name, website=f"https://{name.lower()}.com", tier=tier)
        companies.add_board(conn, name, "greenhouse", ref)
    companies.add_company(conn, "Wday", website="https://wday.com", tier=1)
    resolve.save_resolution(conn, resolve.Resolution(
        company="Wday", outcome="unsupported", job_system="icims",
        job_system_url="https://wday.icims.com/x", careers_url="https://wday.com/careers"))
    companies.add_company(conn, "Mystery", tier=2)

    def eng(job):
        role = mk(job, "Software Engineer")
        return type(role)(**{**role.to_dict(), "department": "Engineering"})
    a = [mk("a1", "Senior Product Manager", "New York"), mk("a2", "Product Manager"), eng("a3")]
    script = Scripted(
        alpha=[a, a + [mk("a4", "Group Product Manager", "Remote")], [a[0], a[2], mk("a4", "Group Product Manager", "Remote")]],
        beta=[[mk("b1", "Product Manager")], [mk("b1", "Product Manager"), mk("b2", "Staff Product Manager")],
              [mk("b1", "Product Manager"), mk("b2", "Staff Product Manager")]],
        gamma=[[mk("g1", "Product Manager Intern")], SourceError("HTTP 500"), [mk("g1", "Product Manager Intern")]])
    config = isolated_db_path.parent / "ranking.toml"
    config.write_text(SMALL_CONFIG.replace("size = 3", "size = 10"))
    clock = Clock()                      # runs on 2026-01-01, -02, -03
    for run in range(1, 4):
        clock.start()
        scout.run_scout(conn, adapter_for=script, clock=clock)
        ranking.build_shortlist(conn, ranking.load_config(config),
                                now=datetime(2026, 1, run, 12, tzinfo=timezone.utc))
    ids = {r["source_job_id"]: r["id"] for r in conn.execute("SELECT id, source_job_id FROM roles")}
    return Session(mcp_server.build_server(isolated_db_path)), ids, conn


# --- the tool list -----------------------------------------------------------

def test_tool_list_and_descriptions_are_written_for_a_newcomer(world):
    session, _, _ = world
    tools = {t.name: t for t in session.tools()}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for name, tool in tools.items():
        assert len(tool.description) > 150, name                   # says what it is for and when to use it
        assert tool.annotations.read_only_hint is (name in READ_TOOLS), name
        assert tool.annotations.open_world_hint is False, name    # no tool reaches outside the database
        for param, schema in tool.input_schema["properties"].items():
            assert schema.get("description"), f"{name}.{param} has no description"
    assert "role_id" in tools["get_role"].description or "role_id" in json.dumps(tools["get_role"].input_schema)
    instructions = mcp_server.INSTRUCTIONS
    assert "never fetches anything itself" in instructions and "role_id" in instructions


def test_read_only_mode_has_no_tool_that_writes(world, isolated_db_path):
    _, ids, conn = world
    session = Session(mcp_server.build_server(isolated_db_path, read_only=True))
    assert {t.name for t in session.tools()} == READ_TOOLS
    assert "mark_role" in session.error("mark_role", role_id=ids["a1"], status="saved")   # no such tool
    assert conn.execute("SELECT COUNT(*) FROM role_marks").fetchone()[0] == 0
    # Reads work, and say which mode the server is in.
    assert session.call("get_shortlist")["roles_on_list"] == 5
    assert session.call("health")["server_mode"] == "read-only"


# --- role tools --------------------------------------------------------------

def test_get_shortlist_flat_by_company_and_new(world):
    session, ids, _ = world
    flat = session.call("get_shortlist")
    assert (flat["list_date"], flat["roles_on_list"], flat["compared_with"], flat["new_count"]) == (
        "2026-01-03", 5, "2026-01-02", 0)
    first = flat["entries"][0]
    assert set(first) == {"rank", "role_id", "score", "company", "tier", "title", "location", "mark", "new", "url"}
    # Scores: a4 85, a1 80, b2 75, a2 60, b1 50 (run-1 roles are baseline, so they get no recency points).
    assert [e["role_id"] for e in flat["entries"]] == [ids[j] for j in ("a4", "a1", "b2", "a2", "b1")]
    assert [e["rank"] for e in flat["entries"]] == [1, 2, 3, 4, 5] and flat["next_offset"] is None
    assert "reasons" not in first                                  # compact unless asked for
    assert session.call("get_shortlist", include_reasons=True)["entries"][0]["reasons"]

    page = session.call("get_shortlist", limit=3, offset=0)
    assert len(page["entries"]) == 3 and page["next_offset"] == 3
    assert [e["rank"] for e in session.call("get_shortlist", limit=3, offset=3)["entries"]] == [4, 5]

    grouped = session.call("get_shortlist", view="by_company")
    assert [(c["company"], c["roles_on_list"], c["best_rank"]) for c in grouped["companies"]] == [
        ("Alpha", 3, 1), ("Beta", 2, 3)]
    assert "company" not in grouped["companies"][0]["roles"][0]    # not repeated inside the group

    # Compared with the first day's list, two roles are new (a4 and b2 appeared in run 2).
    new = session.call("get_shortlist", view="new", since="2026-01-01")
    assert new["compared_with"] == "2026-01-01" and new["new_count"] == 2
    assert {e["role_id"] for e in new["entries"]} == {ids["a4"], ids["b2"]}
    assert all(e["new"] for e in new["entries"])


def test_get_role_returns_the_description_only_when_asked(world):
    session, ids, _ = world
    role = session.call("get_role", role_id=ids["a1"])
    assert (role["company"], role["title"], role["status"], role["tier"]) == (
        "Alpha", "Senior Product Manager", "open", 1)
    assert role["description_available"] is True and "description" not in role
    full = session.call("get_role", role_id=ids["a1"], include_description=True)
    assert full["description"] == "About a1" and full["description_truncated"] is False
    assert "no role with ID 99999" in session.error("get_role", role_id=99999)


def test_search_roles_by_title_company_status_and_date(world):
    session, ids, _ = world
    everything = session.call("search_roles")
    assert everything["total"] == 7 and len(everything["roles"]) == 7
    pm = session.call("search_roles", title=["product manager"], company="alpha")
    assert [r["title"] for r in pm["roles"]] == ["Group Product Manager", "Product Manager", "Senior Product Manager"]
    assert pm["roles"][0]["role_id"] == ids["a4"]
    assert session.call("search_roles", first_seen_since="2026-01-02")["total"] == 2
    assert session.call("search_roles", status="closed")["total"] == 0
    paged = session.call("search_roles", limit=2, offset=6)
    assert len(paged["roles"]) == 1 and paged["next_offset"] is None
    assert session.call("search_roles", limit=2)["next_offset"] == 2
    assert "no company named" in session.error("search_roles", company="Nope")
    assert "description" not in everything["roles"][0]


def test_explain_rank(world):
    session, ids, _ = world
    top = session.call("explain_rank", role_id=ids["a4"])
    assert top["on_shortlist"] is True and top["rank"] == 1
    assert "Tier 1 company (+30)" in top["score_breakdown"]
    intern = session.call("explain_rank", role_id=ids["g1"])
    assert intern["on_shortlist"] is False
    assert intern["why_not_listed"] == 'its title contains "Intern", a phrase you never want'


def test_mark_unmark_and_list_marks(world):
    session, ids, conn = world
    marked = session.call("mark_role", role_id=ids["a1"], status="dismissed", note="wrong team")
    assert (marked["company"], marked["mark"], marked["note"]) == ("Alpha", "dismissed", "wrong team")
    # The shortlist is refreshed at once: the role is gone.
    assert ids["a1"] not in {e["role_id"] for e in session.call("get_shortlist")["entries"]}
    session.call("mark_role", role_id=ids["b2"], status="saved")
    assert [e["mark"] for e in session.call("get_shortlist")["entries"] if e["role_id"] == ids["b2"]] == ["saved"]

    marks = session.call("list_marks")
    assert marks["total"] == 2 and {m["mark"] for m in marks["marks"]} == {"dismissed", "saved"}
    assert [m["role_id"] for m in session.call("list_marks", status="saved")["marks"]] == [ids["b2"]]

    undone = session.call("unmark_role", role_id=ids["a1"])
    assert undone["removed_mark"] == "dismissed"
    assert ids["a1"] in {e["role_id"] for e in session.call("get_shortlist")["entries"]}
    assert session.call("unmark_role", role_id=ids["a1"])["note"] == "the role had no mark"
    assert "no role with ID" in session.error("mark_role", role_id=4242, status="saved")


# --- company tools -----------------------------------------------------------

def test_list_companies_with_mapping_state(world):
    session, _, _ = world
    listed = session.call("list_target_companies")
    by_name = {c["name"]: c for c in listed["companies"]}
    assert listed["total"] == 5
    assert (by_name["Alpha"]["coverage"], by_name["Alpha"]["boards"], by_name["Alpha"]["open_roles"]) == (
        "covered", ["greenhouse/alpha"], 4)
    assert (by_name["Wday"]["coverage"], by_name["Wday"]["job_system"]) == ("unsupported", "icims")
    assert by_name["Mystery"]["coverage"] == "unidentified"
    assert {c["name"] for c in session.call("list_target_companies", coverage="not_covered")["companies"]} == {
        "Wday", "Mystery"}
    assert session.call("list_target_companies", limit=2)["next_offset"] == 2


def test_add_deactivate_reactivate_and_map(world):
    session, _, conn = world
    added = session.call("add_target_company", name="Delta", website="https://delta.com", tier=2, tags=["ai"])
    assert added["active"] is True and added["boards"] == []
    assert "already exists" in session.error("add_target_company", name="delta")

    mapped = session.call("map_board", company="delta", source="lever", board_ref="delta-co")
    assert mapped == {"company": "Delta", "boards": ["lever/delta-co"], "next": "fetched at the next scout"}
    assert "already mapped to 'Delta'" in session.error("map_board", company="Beta", source="lever", board_ref="delta-co")
    assert session.error("map_board", company="Delta", source="icims", board_ref="x")   # not a supported source

    assert session.call("deactivate_company", name="Alpha") == {"name": "Alpha", "active": False}
    inactive = session.call("list_target_companies", coverage="inactive")
    assert [(c["name"], c["coverage"]) for c in inactive["companies"]] == [("Alpha", "inactive")]
    # Its open roles leave default searches.
    assert session.call("search_roles", company="Alpha")["total"] == 0
    assert session.call("search_roles", company="Alpha", include_inactive=True)["total"] == 4
    assert session.call("reactivate_company", name="alpha") == {"name": "Alpha", "active": True}
    assert "no company named" in session.error("deactivate_company", name="Nope")


def test_record_job_system_gaps_and_brief(world):
    session, _, conn = world
    before = session.call("coverage_gaps")
    assert before["summary"] == "1 build candidate, 1 unidentified"
    assert [(g["system"], g["build_candidate"], g["why"]) for g in before["job_systems"]] == [
        ("icims", True, "tier 1: Wday")]
    assert [u["company"] for u in before["unidentified"]] == ["Mystery"]

    recorded = session.call("record_job_system", company="mystery", system="Rippling",
                            url="https://ats.rippling.com/mystery/jobs", note="checked in a browser")
    assert recorded == {"company": "Mystery", "job_system": "rippling",
                        "url": "https://ats.rippling.com/mystery/jobs", "recorded_by_hand": True}
    after = session.call("coverage_gaps")
    assert after["unidentified"] == []
    rippling = next(g for g in after["job_systems"] if g["system"] == "rippling")
    assert rippling["companies"][0]["recorded_by_hand"] is True
    assert "map the board instead" in session.error(
        "record_job_system", company="Mystery", system="greenhouse", url="https://x.com")

    brief = session.call("connector_brief", system="icims")
    assert brief["brief_markdown"].startswith("# Connector brief: icims")
    assert "### Wday (tier 1)" in brief["brief_markdown"]
    assert "Systems with gaps" in session.error("connector_brief", system="taleo")


# --- trends and health -------------------------------------------------------

def test_hiring_trends_for_one_company_and_all(world):
    session, _, _ = world
    alpha = session.call("hiring_trends", company="Alpha", start="2026-01-01", end="2026-01-31")
    c = alpha["company"]
    # Run 1 is the baseline (3 roles, not counted as opened); a4 opened in run 2; a2 only missed once.
    assert (c["open_start"], c["open_end"], c["net_change"], c["opened"], c["closed"]) == (3, 4, 1, 1, 0)
    assert (c["snapshots"], c["baseline_in_range"], c["first_snapshot"], c["last_snapshot"]) == (
        3, True, "2026-01-01", "2026-01-03")
    assert alpha["by_department"] == [
        {"department": "Product", "open_start": 2, "open_end": 3, "change": 1},
        {"department": "Engineering", "open_start": 1, "open_end": 1, "change": 0}]

    # A range that starts after the baseline.
    later = session.call("hiring_trends", company="alpha", start="2026-01-02", end="2026-01-03")
    assert (later["company"]["open_start"], later["company"]["opened"], later["company"]["baseline_in_range"]) == (
        4, 1, False)

    everyone = session.call("hiring_trends", start="2026-01-01", end="2026-01-03")
    assert everyone["companies_with_data"] == 3
    assert everyone["totals"] == {"open_start": 5, "open_end": 7, "net_change": 2, "opened": 2, "closed": 0}
    assert [(r["company"], r["opened"]) for r in everyone["companies"]] == [
        ("Alpha", 1), ("Beta", 1), ("Gamma", 0)]
    assert everyone["by_department"][0] == {"department": "Product", "open_start": 4, "open_end": 6, "change": 2}
    assert session.call("hiring_trends", start="2026-01-01", end="2026-01-03", limit=1)["next_offset"] == 1

    empty = session.call("hiring_trends", start="2025-01-01", end="2025-01-31")
    assert empty["totals"] is None and "No snapshots" in empty["note"]
    assert "no company named" in session.error("hiring_trends", company="Nope")
    assert "start must not be after end" in session.error("hiring_trends", start="2026-02-01", end="2026-01-01")


def test_health_matches_rolescout_status(world, capsys):
    session, _, conn = world
    health = session.call("health")
    assert cli.main(["status", "--json"]) in (0, 1)
    status = json.loads(capsys.readouterr().out)
    assert health["healthy"] == status["healthy"] and health["attention"] == status["attention"]
    assert health["open_roles"] == status["open_roles"] == 7
    assert health["last_run"]["started_at"] == status["last_run"]["started_at"] == "2026-01-03T00:00:00Z"
    assert (health["last_run"]["companies_ok"], health["last_run"]["boards_failed"]) == (3, 0)
    assert health["coverage_gaps"] == status["gaps"]["summary"]
    assert health["next_scheduled_run"] is None and health["server_mode"] == "read-write"


# --- the server never reaches out --------------------------------------------

def test_server_starts_no_scout_and_makes_no_request(world, monkeypatch):
    """conftest already blocks sockets; here anything that could fetch is made to explode too."""
    session, ids, conn = world

    def forbidden(*args, **kwargs):
        raise AssertionError("the MCP server must not fetch or scout")
    monkeypatch.setattr(scout, "run_scout", forbidden)
    monkeypatch.setattr(resolve.Resolver, "resolve", forbidden)
    monkeypatch.setattr("requests.Session.request", forbidden)
    runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    session.call("get_shortlist")
    session.call("health")
    session.call("coverage_gaps")
    session.call("add_target_company", name="Echo", website="https://echo.com")
    session.call("map_board", company="Echo", source="ashby", board_ref="echo")
    session.call("mark_role", role_id=ids["b1"], status="saved")
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs


# --- HTTP: token and binding -------------------------------------------------

TOKEN = "a-long-enough-test-token-123456"


def asgi_request(app, headers):
    """Send one HTTP request through an ASGI app; returns (status, response headers, reached inner app)."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)
    scope = {"type": "http", "method": "POST", "path": "/mcp", "raw_path": b"/mcp", "query_string": b"",
             "http_version": "1.1", "scheme": "http", "server": ("127.0.0.1", 8765),
             "client": ("127.0.0.1", 5555), "headers": headers}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict(start["headers"])


def test_http_request_without_the_token_is_rejected():
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["type"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    gate = mcp_server.BearerTokenGate(inner, TOKEN)

    status, headers = asgi_request(gate, [])
    assert status == 401 and headers[b"www-authenticate"] == b"Bearer" and reached == []
    for wrong in (b"Bearer wrong-token-wrong-token", b"Bearer " + TOKEN.encode() + b"x",
                  TOKEN.encode(), b"Basic " + TOKEN.encode(), b"bearer"):
        assert asgi_request(gate, [(b"authorization", wrong)])[0] == 401
    assert reached == []                                   # nothing got past the gate

    assert asgi_request(gate, [(b"authorization", b"Bearer " + TOKEN.encode())])[0] == 200
    assert reached == ["http"]
    with pytest.raises(ValueError, match="at least 16"):
        mcp_server.BearerTokenGate(inner, "short")


def test_the_real_http_app_rejects_a_request_without_the_token(world, isolated_db_path):
    app = mcp_server.http_app(mcp_server.build_server(isolated_db_path), TOKEN, host="127.0.0.1", port=8765)
    assert isinstance(app, mcp_server.BearerTokenGate)          # the gate is the outermost layer
    assert asgi_request(app, [(b"host", b"127.0.0.1:8765")])[0] == 401
    assert asgi_request(app, [(b"host", b"127.0.0.1:8765"), (b"authorization", b"Bearer nope-nope-nope-nope")])[0] == 401


def test_allowed_hosts_follow_the_bind_address():
    assert mcp_server.allowed_hosts("127.0.0.1", 8765) == ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"]
    tailnet = mcp_server.allowed_hosts("100.64.0.10", 9000, ["mac.tail1234.ts.net", "mac:9000"])
    assert tailnet == ["127.0.0.1:9000", "localhost:9000", "[::1]:9000", "100.64.0.10:9000",
                       "mac.tail1234.ts.net:9000", "mac:9000"]


def test_serve_command_refuses_http_without_a_token_and_defaults_to_localhost(world, capsys, monkeypatch):
    monkeypatch.delenv("ROLESCOUT_MCP_TOKEN", raising=False)
    assert cli.main(["serve", "--http"]) == 1
    assert "HTTP needs a token" in capsys.readouterr().err
    monkeypatch.setenv("ROLESCOUT_MCP_TOKEN", "short")
    assert cli.main(["serve", "--http"]) == 1
    assert "at least 16 characters" in capsys.readouterr().err

    started = {}
    monkeypatch.setenv("ROLESCOUT_MCP_TOKEN", TOKEN)
    monkeypatch.setattr(mcp_server, "serve_http", lambda server, token, **kw: started.update(kw, token=token))
    assert cli.main(["serve", "--http"]) == 0
    assert (started["host"], started["port"], started["token"]) == ("127.0.0.1", 8765, TOKEN)
    assert cli.main(["serve", "--http", "--host", "100.64.0.10", "--port", "9000", "--read-only"]) == 0
    assert (started["host"], started["port"]) == ("100.64.0.10", 9000)
    assert "read-only" in capsys.readouterr().err

    assert cli.main(["serve", "--port", "9000"]) == 1                 # HTTP options without --http
    assert cli.main(["serve", "--db", "/nonexistent/rolescout.db"]) == 1
    assert "no database at" in capsys.readouterr().err


def test_importing_the_module_does_not_need_the_sdk():
    """`rolescout serve` must be able to say "install the extra" instead of crashing."""
    import subprocess
    import sys
    code = ("import sys; sys.modules['mcp'] = None; sys.modules['pydantic'] = None\n"
            "from rolescout import mcp_server\n"
            "try:\n    mcp_server.build_server('x.db')\n"
            "except RuntimeError as exc:\n    print(exc)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert 'pip install "rolescout[mcp]"' in out.stdout, out.stderr
