"""An MCP server over the local rolescout database.

Agents get the same things the CLI offers: the shortlist, roles, marks,
companies, coverage gaps, hiring trends and health. The server only ever
reads and writes the local database. It never starts a scout and never
makes a network request.

Needs the optional dependency: pip install "rolescout[mcp]"
"""

import hmac
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from rolescout import candidates as cands
from rolescout import shortlist as ranking
from rolescout.adapters import ADAPTERS
from rolescout.companies import CompanyError, add_board, add_company, get_company, list_companies, set_active
from rolescout.db import SchemaError, connect
from rolescout.gaps import GapsError, classify, connector_brief, gaps_report, summary_line
from rolescout.ops import collect_status
from rolescout.query import query_roles
from rolescout.resolve import set_job_system
from rolescout.schedule import read_schedule
from rolescout.shortlist import ConfigError, MarkError
from rolescout.trends import hiring_trends

TOKEN_ENV_VAR = "ROLESCOUT_MCP_TOKEN"
MIN_TOKEN_CHARS = 16
DEFAULT_PORT = 8765
MAX_PAGE = 100
MAX_DESCRIPTION_CHARS = 20_000

WRITE_TOOLS = ("mark_role", "unmark_role", "add_company", "deactivate_company",
               "reactivate_company", "map_board", "record_job_system")

INSTRUCTIONS = """\
rolescout tracks open job postings at a personal list of target companies. A scheduled job
("scout") fetches every open role from each company's job board once a day into a local
database; this server answers questions from that database. It never fetches anything itself,
so data is as fresh as the last scout (see the health tool).

Key ideas:
- A role is one job posting. Its role_id is a small integer that never changes; every tool that
  returns roles includes it, and tools that act on a role take it.
- The shortlist is a ranked list of the open roles worth looking at first (100 by default),
  rebuilt after each scout from the owner's ranking rules. Start there for "what should I look at".
- Marks record the owner's decision on a role: dismissed (never show again), saved (keep,
  flagged), applied (never show again; description kept after the role closes).
- A company is identified by its name. Tier 1 is highest priority. A company is fetched only
  if it is mapped to a job board on a supported source (greenhouse, lever, ashby, workday).
- Coverage gaps are companies that cannot be fetched yet: on an unsupported job system, on a
  supported one with no board mapped, or unidentified.

Results are compact and paged: pass limit and offset, and ask for descriptions or reasons only
when you need them. Dates are YYYY-MM-DD; timestamps are UTC.
"""


def _token_from_env_or_file(token_file: str | None) -> str | None:
    if token_file:
        return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
    return (os.environ.get(TOKEN_ENV_VAR) or "").strip() or None


class BearerTokenGate:
    """ASGI wrapper: every HTTP request must carry ``Authorization: Bearer <token>``.

    One fixed token, compared in constant time. Everything that is not an HTTP
    request (the ASGI lifespan messages) passes straight through, so the
    wrapped app still starts and stops normally.
    """

    def __init__(self, app, token: str) -> None:
        if len(token) < MIN_TOKEN_CHARS:
            raise ValueError(f"the token must be at least {MIN_TOKEN_CHARS} characters")
        self.app = app
        self._expected = b"Bearer " + token.encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
        if hmac.compare_digest(supplied, self._expected):
            await self.app(scope, receive, send)
            return
        body = b'{"error":"unauthorized","detail":"send Authorization: Bearer <token>"}'
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"), (b"www-authenticate", b"Bearer"),
            (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit), MAX_PAGE)), max(0, int(offset))


def _next(offset: int, shown: int, total: int) -> int | None:
    return offset + shown if offset + shown < total else None


def build_server(db_path: str | os.PathLike, config: str | os.PathLike | None = None, *,
                 read_only: bool = False):
    """Create the MCP server for one database. In ``read_only`` mode the tools
    that change anything are not registered at all."""
    try:
        from mcp.server import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        from mcp.types import ToolAnnotations
        from pydantic import Field  # comes with the SDK
    except ImportError as exc:
        raise RuntimeError(
            'the MCP server needs the optional dependency: pip install "rolescout[mcp]" '
            f"({exc})") from None

    db_path = Path(db_path)
    config_path = ranking.config_path(db_path, config)
    server = MCPServer("rolescout", instructions=INSTRUCTIONS)
    reads = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    writes = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

    @contextmanager
    def database():
        """A connection for one tool call (tools run on worker threads). Errors
        an agent can act on are turned into tool errors with the same message."""
        try:
            conn = connect(db_path)
        except (SchemaError, sqlite3.Error) as exc:
            raise ToolError(f"cannot open the rolescout database: {exc}") from None
        try:
            if read_only:
                conn.execute("PRAGMA query_only = ON")
            yield conn
        except (CompanyError, MarkError, ConfigError, GapsError, ValueError, cands.CandidateError,
                cands.ConfigError) as exc:
            raise ToolError(str(exc)) from None
        finally:
            conn.close()

    def load_ranking():
        return ranking.load_config(config_path)

    def rebuild_quietly(conn) -> None:
        try:
            if config_path.exists():
                ranking.build_shortlist(conn, load_ranking())
        except Exception:
            pass  # the mark itself succeeded; the list refreshes at the next scout

    RoleId = Annotated[int, Field(description="The role's role_id: the integer every role-returning tool includes.", ge=1)]
    Limit = Annotated[int, Field(description=f"Maximum rows to return (1-{MAX_PAGE}).", ge=1, le=MAX_PAGE)]
    Offset = Annotated[int, Field(description="Rows to skip; pass the previous result's next_offset to get the next page.", ge=0)]
    CompanyName = Annotated[str, Field(description="The company's name exactly as list_companies shows it (case does not matter).")]

    # ------------------------------------------------------------------ roles

    @server.tool(title="Get the shortlist", annotations=reads)
    def get_shortlist(
        view: Annotated[Literal["flat", "by_company", "new"], Field(description=(
            "flat: roles in rank order. by_company: the same roles grouped by company, companies "
            "with the best-ranked role first. new: only roles that were not on the previous day's list."))] = "flat",
        limit: Limit = 25,
        offset: Offset = 0,
        since: Annotated[str | None, Field(description=(
            "For view=new: compare with the list of this date (YYYY-MM-DD) instead of the previous "
            "one. Lists are kept for 30 days."))] = None,
        include_reasons: Annotated[bool, Field(description="Include the plain-language reasons behind each score.")] = False,
    ) -> dict[str, Any]:
        """The ranked shortlist: the open roles the owner should look at first.

        Use this first for questions like "what should I look at today" or "anything new".
        The list is rebuilt after every daily scout from the owner's ranking rules; rank 1 is
        best. Each entry has role_id, rank, score, company, tier, title, location, mark
        ("saved" or null) and whether it is new since the previous list. For view=by_company,
        limit and offset page over companies. Follow up with get_role for a description or
        explain_rank for the scoring.
        """
        with database() as conn:
            current = ranking.latest_shortlist(conn)
            if current is None:
                return {"list_date": None, "entries": [], "note": (
                    "No shortlist has been built yet. It is built at the end of a scout once a "
                    "ranking config exists (rolescout shortlist init).")}
            previous, new_ids = ranking.new_since(conn, current["id"], since)
            entries = ranking.shortlist_entries(conn, current["id"])
            held = ranking.held_back(conn, current["id"])
            limit, offset = _page(limit, offset)

            def compact(e: dict, with_company: bool = True) -> dict:
                row = {"rank": e["rank"], "role_id": e["id"], "score": e["score"]}
                if with_company:
                    row.update(company=e["company"], tier=e["tier"])
                row.update(title=e["title"], location=e["location"], mark=e["mark"],
                           new=bool(previous) and e["id"] in new_ids, url=e["url"])
                if include_reasons:
                    row["reasons"] = e["reasons"]
                return row

            result: dict[str, Any] = {
                "list_date": current["list_date"], "built_at": current["built_at"],
                "roles_on_list": len(entries), "roles_that_qualify": current["eligible"],
                "held_back_by_company_cap": len(held),
                "compared_with": previous["list_date"] if previous else None,
                "new_count": len(new_ids) if previous else None,
            }
            if view == "by_company":
                held_by: dict[str, int] = {}
                for h in held:
                    held_by[h["company"]] = held_by.get(h["company"], 0) + 1
                groups: dict[str, list[dict]] = {}
                for e in entries:  # already in rank order, so groups are ordered by best rank
                    groups.setdefault(e["company"], []).append(e)
                names = list(groups)
                page = names[offset:offset + limit]
                result["companies"] = [{
                    "company": name, "tier": groups[name][0]["tier"],
                    "roles_on_list": len(groups[name]), "best_rank": groups[name][0]["rank"],
                    "held_back_by_cap": held_by.get(name, 0),
                    "roles": [compact(e, with_company=False) for e in groups[name]]} for name in page]
                result["next_offset"] = _next(offset, len(page), len(names))
                return result
            if view == "new":
                if previous is None:
                    result["note"] = "There is no earlier list to compare with, so nothing can be new yet."
                entries = [e for e in entries if e["id"] in new_ids] if previous else []
            page = entries[offset:offset + limit]
            result["entries"] = [compact(e) for e in page]
            result["next_offset"] = _next(offset, len(page), len(entries))
            return result

    @server.tool(title="Get one role", annotations=reads)
    def get_role(
        role_id: RoleId,
        include_description: Annotated[bool, Field(description=(
            "Include the full job description text. Leave false unless you need to read it; "
            "descriptions run to several thousand characters."))] = False,
    ) -> dict[str, Any]:
        """Everything stored about one role: company, title, department, location, workplace
        type, URL, posted date, compensation text, when it was first and last seen, whether it
        is open or closed, and the owner's mark on it.

        Use it after get_shortlist or search_roles when you need the details or the description
        of a specific role. The description of a closed role is deleted unless the owner marked
        the role saved or applied; description_available says whether there is one.
        """
        with database() as conn:
            role = ranking.get_role(conn, role_id)
            extra = conn.execute(
                "SELECT r.source, r.last_seen_at, d.description FROM roles r "
                "LEFT JOIN role_descriptions d ON d.role_id = r.id WHERE r.id = ?", (role_id,)).fetchone()
            result = {
                "role_id": role["id"], "company": role["company"], "tier": role["tier"],
                "company_active": bool(role["active"]), "title": role["title"],
                "department": role["department"], "location": role["location"],
                "workplace_type": role["workplace_type"], "url": role["url"],
                "date_posted": role["date_posted"], "compensation_text": role["compensation_text"],
                "source": extra["source"], "status": "closed" if role["closed_at"] else "open",
                "first_seen_at": role["first_seen_at"], "last_seen_at": extra["last_seen_at"],
                "closed_at": role["closed_at"], "mark": role["mark"], "mark_note": role["mark_note"] or None,
                "description_available": extra["description"] is not None,
            }
            if include_description and extra["description"] is not None:
                text = extra["description"]
                result["description"] = text[:MAX_DESCRIPTION_CHARS]
                result["description_truncated"] = len(text) > MAX_DESCRIPTION_CHARS
            return result

    @server.tool(title="Search roles", annotations=reads)
    def search_roles(
        title: Annotated[list[str] | None, Field(description=(
            'Title phrases; a role matches if its title contains any one of them, ignoring case. '
            'Example: ["product manager", "program manager"].'))] = None,
        company: Annotated[str | None, Field(description="Only this company (exact name, case does not matter).")] = None,
        status: Annotated[Literal["open", "closed", "all"], Field(description="open (default), closed, or all.")] = "open",
        first_seen_since: Annotated[str | None, Field(description=(
            "Only roles first seen on or after this date (YYYY-MM-DD). This is when rolescout first "
            "saw the role, which for a newly added company is the day it was first fetched."))] = None,
        include_inactive: Annotated[bool, Field(description=(
            "Also return open roles of deactivated companies. They are no longer refreshed, so "
            "'open' may be stale."))] = False,
        limit: Limit = 25,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Search every stored role, not just the shortlist, by title, company, status and date.

        Use it for questions the shortlist does not answer: "does Stripe have any design roles",
        "what closed at Anthropic", "what appeared since Monday". Results are ordered by company
        then title and include role_id for follow-up calls. Descriptions are not searched and
        not returned; use get_role for one.
        """
        with database() as conn:
            if company:
                company = get_company(conn, company)["name"]
            limit, offset = _page(limit, offset)
            common = dict(company=company, title=title, status=status, since=first_seen_since,
                          include_inactive=include_inactive)
            total = len(query_roles(conn, **common))
            rows = query_roles(conn, **common, limit=limit, offset=offset)
            return {
                "total": total,
                "roles": [{"role_id": r["id"], "company": r["company"], "title": r["title"],
                           "department": r["department"], "location": r["location"],
                           "status": r["status"], "date_posted": r["date_posted"],
                           "first_seen": r["first_seen_at"][:10],
                           "closed": r["closed_at"][:10] if r["closed_at"] else None,
                           "url": r["url"]} for r in rows],
                "next_offset": _next(offset, len(rows), total),
            }

    @server.tool(title="Explain a role's rank", annotations=reads)
    def explain_rank(role_id: RoleId) -> dict[str, Any]:
        """Why a role ranks where it does on the shortlist, or why it is not on it.

        Returns the role's score, one line for every scoring rule (including the ones that gave
        no points), its rank if listed, and otherwise the reason it is absent: closed, company
        deactivated, marked dismissed or applied, title rules, held back by the per-company cap,
        or simply outscored. Use it when the owner asks "why is this here" or "why isn't X on
        the list".
        """
        with database() as conn:
            info = ranking.explain(conn, role_id, load_ranking())
            role = info["role"]
            return {
                "role_id": role["id"], "company": role["company"], "title": role["title"],
                "on_shortlist": info["rank"] is not None, "rank": info["rank"],
                "list_date": info["list_date"], "score": info["score"], "score_breakdown": info["detail"],
                "why_not_listed": info["why_not"] or None,
                "held_back_by_company_cap": info["held_back_by_cap"], "mark": role["mark"],
                "ranking_rules_changed_since_list_was_built": info["config_changed"],
            }

    @server.tool(title="List marked roles", annotations=reads)
    def list_marks(
        status: Annotated[Literal["dismissed", "saved", "applied"] | None, Field(description="Only marks of this kind. Omit for all.")] = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """The roles the owner has marked, newest mark first: dismissed, saved or applied, with
        the note, the date, and whether the role is still open.

        Use it for "what have I applied to", "show my saved roles", or before suggesting a role,
        to check it has not already been dismissed.
        """
        with database() as conn:
            marks = ranking.list_marks(conn, status)
            limit, offset = _page(limit, offset)
            page = marks[offset:offset + limit]
            return {
                "total": len(marks),
                "marks": [{"role_id": m["id"], "mark": m["status"], "note": m["note"] or None,
                           "marked": m["marked_at"][:10], "company": m["company"], "title": m["title"],
                           "role_status": "closed" if m["closed_at"] else "open",
                           "description_kept": bool(m["has_description"]), "url": m["url"]} for m in page],
                "next_offset": _next(offset, len(page), len(marks)),
            }

    # -------------------------------------------------------------- companies

    @server.tool(title="List companies", annotations=reads)
    def list_target_companies(
        coverage: Annotated[Literal["all", "covered", "not_covered", "inactive"], Field(description=(
            "all (default); covered: has a working job board; not_covered: active but cannot be "
            "fetched yet; inactive: deactivated."))] = "all",
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """The owner's target companies and whether rolescout can fetch each one.

        Each row has the name (the identifier other tools take), tier (1 is highest priority),
        whether it is active, its job boards as "source/board_ref", open role count, and a
        coverage state: covered, unsupported (on a job system with no adapter; job_system says
        which), needs_mapping (on a supported system but no board mapped), unidentified, or
        inactive. Use it to answer "which companies am I tracking" and to find exact names.
        """
        with database() as conn:
            states = {e["company"]: e for e in classify(conn)}
            rows = []
            for c in list_companies(conn):
                state = states.get(c["name"])
                rows.append({
                    "name": c["name"], "tier": c["tier"], "active": c["active"],
                    "coverage": state["state"] if state else "inactive",
                    "boards": [f"{b['source']}/{b['ref']}" for b in c["boards"]],
                    "open_roles": c["open_roles"],
                    "job_system": state["system"] if state and state["state"] != "covered" else None,
                    "failing_boards": state["failing_boards"] if state else [],
                    "website": c["website"], "tags": c["tags"]})
            if coverage == "covered":
                rows = [r for r in rows if r["coverage"] == "covered"]
            elif coverage == "not_covered":
                rows = [r for r in rows if r["coverage"] not in ("covered", "inactive")]
            elif coverage == "inactive":
                rows = [r for r in rows if not r["active"]]
            limit, offset = _page(limit, offset)
            page = rows[offset:offset + limit]
            return {"total": len(rows), "companies": page, "next_offset": _next(offset, len(page), len(rows))}

    @server.tool(title="Coverage gaps", annotations=reads)
    def coverage_gaps() -> dict[str, Any]:
        """Which target companies rolescout cannot fetch, and which connector is worth building next.

        Unsupported companies are grouped by the job system they use (icims, workable, ...).
        A system is a build candidate when three or more companies use it or a tier-1 company
        does. Also lists companies on a supported system that still need a board mapped
        (fix with map_board) and companies whose job system is unknown (fix with
        record_job_system once someone has looked). Use connector_brief for the detail on one
        system.
        """
        with database() as conn:
            report = gaps_report(conn)
            return {
                "summary": summary_line(report),
                "active_companies": report["active_companies"], "covered": report["covered"],
                "job_systems": [{
                    "system": g["system"], "build_candidate": g["build_candidate"], "why": g["why"] or None,
                    "companies": [{"company": m["company"], "tier": m["tier"], "url": m["url"],
                                   "recorded_by_hand": m["manual"]} for m in g["companies"]]}
                    for g in report["systems"]],
                "needs_mapping": [{
                    "company": e["company"], "tier": e["tier"], "system": e["system"],
                    "candidates": [f"{c['source']}/{c['ref']}: {c['detail']}" for c in e["candidates"]],
                    "note": e["notes"][-1] if e["notes"] and not e["candidates"] else None}
                    for e in report["needs_mapping"]],
                "unidentified": [{"company": e["company"], "tier": e["tier"],
                                  "careers_page": e["careers_url"] or e["website"], "why": e["reason"]}
                                 for e in report["unidentified"]],
            }

    @server.tool(name="connector_brief", title="Connector brief", annotations=reads)
    def connector_brief_tool(
        system: Annotated[str, Field(description="A job system named by coverage_gaps, e.g. workable.")],
    ) -> dict[str, Any]:
        """A Markdown brief for building an adapter for one unsupported job system: the affected
        companies with tiers, their careers and job-system URLs, what an adapter must implement,
        and open questions. Use it when the owner wants to plan or spec a new connector.
        """
        with database() as conn:
            return {"system": system.strip().lower(), "brief_markdown": connector_brief(conn, system)}

    @server.tool(name="hiring_trends", title="Hiring trends", annotations=reads)
    def hiring_trends_tool(
        company: Annotated[str | None, Field(description="One company's name. Omit for all companies together.")] = None,
        start: Annotated[str | None, Field(description="First day of the range, YYYY-MM-DD. Default: 30 days before end.")] = None,
        end: Annotated[str | None, Field(description="Last day of the range, YYYY-MM-DD. Default: today.")] = None,
        limit: Annotated[int, Field(description="For all companies: how many per-company rows to return (1-100).", ge=1, le=MAX_PAGE)] = 20,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """How hiring changed over a date range: open roles at the start and end, and how many
        roles opened and closed in between, overall and by department.

        Use it for "is Anthropic hiring more than last month", "who opened the most roles this
        week", "which departments are growing". Figures come from the snapshot stored after each
        daily scout, so the range can only reach back to when a company was first fetched.
        A company's first fetch (its baseline) is not counted as roles opened. Department rows
        give open counts at the first and last snapshot and the change between them. For all
        companies, per-company rows are sorted by roles opened and paged with limit and offset.
        """
        with database() as conn:
            return hiring_trends(conn, company=company, start=start, end=end, limit=limit, offset=offset)

    @server.tool(title="Health", annotations=reads)
    def health() -> dict[str, Any]:
        """Whether rolescout is running properly, without any network call: when the last scout
        ran and how it went, companies and boards that failed, boards failing more than two
        runs in a row, open role count, database size, coverage gap summary, and when the next
        scheduled run is due.

        Check it first when data looks stale or the owner asks "is it working". healthy is false
        when something needs attention, and attention lists what.
        """
        with database() as conn:
            status = collect_status(conn, schedule=read_schedule())
            schedule = status.get("schedule") or {}
            last = status["last_run"]
            return {
                "healthy": status["healthy"], "attention": status["attention"],
                "last_run": None if last is None else {
                    k: last[k] for k in ("started_at", "status", "hours_ago", "companies_ok",
                                         "companies_failed", "boards_ok", "boards_failed",
                                         "roles_opened", "roles_closed")} | {
                    "failed": [f"{f['name']} ({f['source']}/{f['ref']}): {f['error']}" for f in last["failed"]]},
                "scout_running_now": status["scout_running"],
                "failing_boards": [f"{b['company']} ({b['source']}/{b['ref']}): failed "
                                   f"{b['failed_runs_in_a_row']} runs in a row" for b in status["failing_boards"]],
                "open_roles": status["open_roles"], "companies_active": status["companies_active"],
                "companies_mapped": status["companies_mapped"],
                "database_megabytes": round(status["database_bytes"] / 1_000_000, 1),
                "coverage_gaps": status["gaps"]["summary"],
                "next_scheduled_run": schedule.get("next_run") if schedule.get("loaded") else None,
                "schedule_note": None if schedule.get("loaded") else (
                    "no daily run is scheduled on this machine" if schedule.get("supported")
                    else "scheduling is only built for macOS"),
                "server_mode": "read-only" if read_only else "read-write",
            }

    CandidateRef = Annotated[str, Field(description=(
        "The candidate's website domain (e.g. example.com) or its name, as list_candidates shows them."))]

    def candidate_row(c: dict) -> dict:
        return {"name": c["name"], "domain": c["domain"], "status": c["state"],
                "why": (f"tracked as {c['tracked_as']}" if c["state"] == "tracked"
                        else c["dismiss_note"] or "dismissed" if c["state"] == "dismissed" else c["reason"]),
                "stage": c["stage"], "employees": c["employees"], "sectors": c["sectors"][:5],
                "matching_open_roles": c["matching_roles"],
                "boards": [b["name"] or b["url"] for b in c["boards"]]}

    @server.tool(title="List candidate companies", annotations=reads)
    def list_candidates(
        status: Annotated[Literal["eligible", "review", "rejected", "tracked", "dismissed"] | None, Field(description=(
            "eligible: passed every rule, ready to activate. review: a rule could not be decided. "
            "rejected: failed a rule. tracked: already one of the owner's companies. dismissed: the "
            "owner never wants it. Omit for all."))] = None,
        limit: Limit = 25,
        offset: Offset = 0,
    ) -> dict[str, Any]:
        """Companies found on the investor portfolio boards the owner follows, that are not yet
        (or already) tracked. Each has a name, website domain, funding stage, employee count,
        sectors, the boards that list it, and a status with the reason.

        Use it for "any new companies worth adding" or "what did the last harvest find". These
        are companies, not jobs: no role is stored for a candidate until it is activated and its
        own job board is fetched. Results are ordered by how many boards list the company, then
        by size. The funnel in the result shows how the whole list narrows rule by rule.
        """
        with database() as conn:
            rows = cands.list_candidates(conn, status)
            limit, offset = _page(limit, offset)
            page = rows[offset:offset + limit]
            return {"total": len(rows), "candidates": [candidate_row(c) for c in page],
                    "next_offset": _next(offset, len(page), len(rows)), "funnel": cands.funnel(conn)}

    @server.tool(title="Explain a candidate's status", annotations=reads)
    def explain_candidate(candidate: CandidateRef) -> dict[str, Any]:
        """Why a candidate company is eligible, needs review, or was rejected: the result of each
        filter rule in order (size, funding stage, sector, open roles in the wanted locations),
        plus its details and the boards that list it.

        Use it when the owner asks "why was X rejected" or before recommending that a candidate
        in review be activated.
        """
        with database() as conn:
            c = cands.get_candidate(conn, candidate)
            return {**candidate_row(c), "website": c["website"], "locations": c["locations"][:8],
                    "sectors": c["sectors"],
                    "rules": [{"rule": rule, **c["checks"][rule]} for rule in cands.RULES if rule in c["checks"]],
                    "first_seen": c["first_seen_at"][:10], "last_checked": (c["last_checked_at"] or "")[:10] or None}

    if read_only:
        return server

    @server.tool(title="Activate a candidate", annotations=writes)
    def activate_candidate(
        candidate: CandidateRef,
        force: Annotated[bool, Field(description=(
            "Activate even though the candidate is rejected or dismissed. Leave false unless the "
            "owner said to override."))] = False,
    ) -> dict[str, Any]:
        """Start tracking a candidate: it becomes one of the owner's target companies at tier 3,
        tagged with the portfolio boards that list it. Its own job board is not known yet; the
        weekly re-check looks for it, and its roles appear after that. Nothing is fetched now.

        Only activate when the owner has asked for that company (or for "the eligible ones").
        Eligible and in-review candidates can be activated; rejected and dismissed ones need force.
        """
        with database() as conn:
            done = cands.activate(conn, cands.get_candidate(conn, candidate), force=force)
            return {**done, "next": "its job board is looked for at the next weekly re-check"}

    @server.tool(title="Dismiss a candidate", annotations=writes)
    def dismiss_candidate(
        candidate: CandidateRef,
        note: Annotated[str, Field(description="Optional: why the owner does not want it.")] = "",
    ) -> dict[str, Any]:
        """Mark a candidate as one the owner never wants, so it stops appearing as eligible or
        in review. Later harvests keep it dismissed. Use it only when the owner has said so;
        the owner can undo it from the command line (rolescout candidates restore NAME).
        """
        with database() as conn:
            c = cands.get_candidate(conn, candidate)
            cands.dismiss(conn, c, note)
            return {"name": c["name"], "domain": c["domain"], "status": "dismissed", "note": note.strip() or None}

    # ------------------------------------------------------------ write tools

    @server.tool(title="Mark a role", annotations=writes)
    def mark_role(
        role_id: RoleId,
        status: Annotated[Literal["dismissed", "saved", "applied"], Field(description=(
            "dismissed: not interested, never shortlist it again. saved: keep it on the shortlist, "
            "flagged. applied: the owner applied; never shortlist it again."))],
        note: Annotated[str, Field(description="Optional note to keep with the mark, e.g. why or who referred.")] = "",
    ) -> dict[str, Any]:
        """Record the owner's decision on a role. A role has one mark at a time; marking again
        replaces it. The shortlist is refreshed immediately, so a dismissed or applied role
        disappears and the next role moves up. Saved and applied roles keep their description
        after the posting closes. Only mark a role when the owner has said so; undo with
        unmark_role.
        """
        with database() as conn:
            role = ranking.mark_role(conn, role_id, status, note)
            rebuild_quietly(conn)
            return {"role_id": role_id, "company": role["company"], "title": role["title"],
                    "mark": status, "note": note.strip() or None,
                    "role_status": "closed" if role["closed_at"] else "open"}

    @server.tool(title="Remove a role's mark", annotations=writes)
    def unmark_role(role_id: RoleId) -> dict[str, Any]:
        """Remove the mark from a role, undoing mark_role. A dismissed or applied role becomes
        eligible for the shortlist again. If the role has already closed, its kept description
        is deleted like any other closed role's.
        """
        with database() as conn:
            was = ranking.unmark_role(conn, role_id)
            role = ranking.get_role(conn, role_id)
            rebuild_quietly(conn)
            return {"role_id": role_id, "company": role["company"], "title": role["title"],
                    "removed_mark": was, "note": None if was else "the role had no mark"}

    @server.tool(title="Add a company", annotations=writes)
    def add_target_company(
        name: Annotated[str, Field(description="The company's name. Must not already exist.")],
        website: Annotated[str, Field(description="Its homepage URL, e.g. https://example.com. Used to find its job board.")] = "",
        tier: Annotated[int | None, Field(description="Priority tier: 1 highest, then 2, 3. Omit if unknown.", ge=1)] = None,
        tags: Annotated[list[str] | None, Field(description="Free-form labels, e.g. [\"ai\", \"fintech\"].")] = None,
    ) -> dict[str, Any]:
        """Add a company to the target list. It is not fetched until it is mapped to a job
        board: either the owner's weekly re-check finds the board from the website, or you map
        it with map_board if you know the board. This tool does not look anything up.
        """
        with database() as conn:
            add_company(conn, name, website=website, tier=tier, tags=tags)
            return {"name": name.strip(), "tier": tier, "active": True, "boards": [],
                    "next": "map_board if the board is known; otherwise the weekly re-check will look for it"}

    @server.tool(title="Deactivate a company", annotations=writes)
    def deactivate_company(name: CompanyName) -> dict[str, Any]:
        """Stop tracking a company without deleting anything: it is no longer fetched, its roles
        leave the shortlist and default searches, and its history is kept. Undo with
        reactivate_company.
        """
        with database() as conn:
            set_active(conn, name, False)
            return {"name": get_company(conn, name)["name"], "active": False}

    @server.tool(title="Reactivate a company", annotations=writes)
    def reactivate_company(name: CompanyName) -> dict[str, Any]:
        """Resume tracking a company that was deactivated with deactivate_company. Its boards
        are fetched again from the next scout, and its roles can return to the shortlist and to
        default searches after that. Nothing is fetched now.
        """
        with database() as conn:
            set_active(conn, name, True)
            return {"name": get_company(conn, name)["name"], "active": True}

    @server.tool(title="Map a company to a job board", annotations=writes)
    def map_board(
        company: CompanyName,
        source: Annotated[Literal["greenhouse", "lever", "ashby", "workday"], Field(description="The job system hosting the board.")],
        board_ref: Annotated[str, Field(description=(
            "The board's identifier on that system: the slug in its URL, e.g. 'anthropic' in "
            "job-boards.greenhouse.io/anthropic, 'spotify' in jobs.lever.co/spotify, 'notion' in "
            "jobs.ashbyhq.com/notion. For workday: TENANT/wdN/SITE (e.g. acme/wd5/Careers) "
            "or any URL on the company's Workday career site."))],
    ) -> dict[str, Any]:
        """Tell rolescout which job board belongs to a company, so the next scout fetches it.

        Use it when coverage_gaps lists a company under needs_mapping and the owner has
        confirmed the board, or when you know a new company's board. A company may have several
        boards; a board can belong to only one company. Nothing is fetched or verified now: a
        wrong reference shows up as a failing board in health after the next scout.
        """
        with database() as conn:
            if source not in ADAPTERS:
                raise ToolError(f"unknown source {source!r}")
            add_board(conn, company, source, board_ref)
            name = get_company(conn, company)["name"]
            boards = [f"{b['source']}/{b['ref']}" for c in list_companies(conn)
                      if c["name"] == name for b in c["boards"]]
            return {"company": name, "boards": boards, "next": "fetched at the next scout"}

    @server.tool(title="Record a company's job system", annotations=writes)
    def record_job_system(
        company: CompanyName,
        system: Annotated[str, Field(description=(
            "The job system's name in lowercase, e.g. icims, rippling, paycom, smartrecruiters; "
            "or custom for a careers site the company built itself. Not greenhouse, lever, "
            "ashby or workday: for those use map_board."))],
        url: Annotated[str, Field(description="The URL where the company's jobs are listed on that system.")],
        note: Annotated[str, Field(description="Optional: how it was identified.")] = "",
    ) -> dict[str, Any]:
        """Record which unsupported job system a company uses, for a company automatic detection
        could not identify (coverage_gaps lists these as unidentified). Use it after you have
        looked at the company's careers page, for example with a browser. The entry feeds the
        coverage gaps report and is never overwritten by automatic detection.
        """
        with database() as conn:
            set_job_system(conn, company, system, url, note)
            return {"company": get_company(conn, company)["name"], "job_system": system.strip().lower(),
                    "url": url.strip(), "recorded_by_hand": True}

    return server


# --- running it --------------------------------------------------------------

def allowed_hosts(host: str, port: int, extra: list[str] | None = None) -> list[str]:
    """Host header values the HTTP server answers to: the address it binds,
    localhost, and any names added by hand (for example a Tailscale name)."""
    names = ["127.0.0.1", "localhost", "[::1]"]
    if host not in ("0.0.0.0", "::", "127.0.0.1", "localhost", "::1"):
        names.append(f"[{host}]" if ":" in host else host)
    hosts = [f"{name}:{port}" for name in names]
    for entry in extra or []:
        entry = entry.strip()
        if entry:
            hosts.append(entry if ":" in entry.rsplit("]", 1)[-1] else f"{entry}:{port}")
    return list(dict.fromkeys(hosts))


def http_app(server, token: str, *, host: str, port: int, allow_hosts: list[str] | None = None):
    """The Streamable HTTP app, answering only requests that carry the token
    and name a host we expect (DNS-rebinding protection stays on)."""
    from mcp.server.transport_security import TransportSecuritySettings

    app = server.streamable_http_app(
        host=host,
        # No per-client session state: a restart of the server never strands a client.
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts(host, port, allow_hosts), allowed_origins=[]))
    return BearerTokenGate(app, token)


def serve_stdio(server) -> None:
    server.run()  # stdio: stdout carries the protocol, so nothing else may print there


def serve_http(server, token: str, *, host: str, port: int, allow_hosts: list[str] | None = None) -> None:
    import uvicorn

    uvicorn.run(http_app(server, token, host=host, port=port, allow_hosts=allow_hosts),
                host=host, port=port, log_level="warning", access_log=False)
