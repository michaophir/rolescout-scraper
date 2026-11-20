"""The ``rolescout`` console command."""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from rolescout import __version__, digest, filters, jobs, overview, profiles, vcs
from rolescout.adapters import ADAPTERS, SourceError, get_adapter
from rolescout.companies import (
    CompanyError, add_board, add_company, board_ref, get_company, import_csv, list_companies,
    remove_board,
    set_active, set_website,
)
from rolescout.db import DB_ENV_VAR, DEFAULT_DB_PATH, SchemaError, connect, resolve_db_path, transaction
from rolescout import schedule as launchd
from rolescout.gaps import (
    GapsError, connector_brief, gaps_report, mark_rechecked, recheck_due, summary_line,
)
from rolescout.ops import BackupError, backup_database, collect_status, failing_boards, rotate_log
from rolescout.query import query_roles
from rolescout.resolve import (
    Resolution, clear_job_system, mapping_report, resolve_companies, set_job_system,
)
from rolescout.scout import BoardResult, ScoutAlreadyRunning, run_scout
from rolescout import shortlist as ranking
from rolescout.shortlist import ConfigError, MarkError
from rolescout.transfer import TransferError, export_companies, import_companies
from rolescout import candidates as cands
from rolescout import portfolio
from rolescout import starter

DESCRIPTION_PREVIEW_CHARS = 400


def format_table(headers: list[str], rows: list[list], max_widths: dict[str, int] | None = None) -> str:
    """Plain left-aligned columns. Cells longer than a column's cap are cut with …."""
    caps = max_widths or {}
    cells = [["" if v is None else str(v) for v in row] for row in rows]
    for row in cells:
        for i, header in enumerate(headers):
            cap = caps.get(header)
            if cap and len(row[i]) > cap:
                row[i] = row[i][:cap - 1] + "…"
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h)
              for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip(),
             "  ".join("-" * w for w in widths)]
    lines += ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells]
    return "\n".join(lines)


# --- fetch ------------------------------------------------------------------

def _cmd_fetch(args: argparse.Namespace) -> int:
    adapter = get_adapter(args.source)
    # Paced like a scout: some sources take more than one request per board.
    if hasattr(getattr(adapter, "session", None), "min_interval"):
        adapter.session.min_interval = 1.0
    started = time.monotonic()
    try:
        roles = adapter.fetch(args.slug)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"{args.source}/{board_ref(args.source, args.slug)}: {len(roles)} open roles"
          + _cost(adapter.last_stats.requests, time.monotonic() - started))
    for note in adapter.last_stats.notes:
        print(f"  note: {note}")
    if not roles:
        return 0
    if not 1 <= args.sample <= len(roles):
        print(f"error: --sample must be between 1 and {len(roles)}", file=sys.stderr)
        return 1

    print(f"\nSample role ({args.sample} of {len(roles)}):")
    for field, value in roles[args.sample - 1].to_dict().items():
        if field == "description" and not args.full and len(value) > DESCRIPTION_PREVIEW_CHARS:
            value = f"{value[:DESCRIPTION_PREVIEW_CHARS]}… [{len(value)} chars; --full to show all]"
        print(f"  {field + ':':<19}{value}")
    return 0


# --- companies --------------------------------------------------------------

def _cmd_companies_import(args) -> int:
    conn = connect(args.db)
    if args.file == "-" or args.file.lower().endswith(".json"):
        try:
            data = json.load(sys.stdin) if args.file == "-" else json.loads(
                Path(args.file).expanduser().read_text(encoding="utf-8"))
        except ValueError as exc:
            raise TransferError(f"{args.file} is not valid JSON: {exc}") from None
        result = import_companies(conn, data)
        print(f"Imported {'standard input' if args.file == '-' else args.file}: "
              f"{result['companies_added']} companies added, {result['companies_updated']} updated, "
              f"{result['boards_added']} boards added.")
        if result["vcs_added"] or result["vcs_updated"] or result["profile_values"] or result["vc_links"]:
            print(f"Also: {result['vcs_added']} VCs added, {result['vcs_updated']} updated, "
                  f"{result['profile_values']} profile values set by hand, {result['vc_links']} VC links made by hand.")
        for warning in result["warnings"]:
            print(f"  warning: {warning}")
        return 0
    added, updated = import_csv(conn, args.file)
    print(f"Imported {args.file}: {added} added, {updated} updated.")
    return 0


def _inside_git_repo(path: Path) -> Path | None:
    for parent in path.resolve().parents:
        if (parent / ".git").exists():
            return parent
    return None


def _cmd_companies_export(args) -> int:
    data = export_companies(connect(args.db))
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if args.file == "-":
        sys.stdout.write(text)
        return 0
    path = Path(args.file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Private to you: the file lists the companies you are targeting.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    boards = sum(len(c["boards"]) for c in data["companies"])
    by_hand = sum(len(c.get("profile") or {}) for c in data["companies"])
    links = sum(len(c.get("vcs") or []) for c in data["companies"])
    print(f"Exported {len(data['companies'])} companies and {boards} boards to {path}")
    if data["vcs"] or by_hand or links:
        print(f"With them: {len(data['vcs'])} VCs, {by_hand} profile values set by hand, {links} VC links made by hand.")
    repo = _inside_git_repo(path)
    if repo:
        print(f"warning: {path} is inside the git repository at {repo}. It is personal data; "
              "move it out or make sure it is ignored before committing.", file=sys.stderr)
    return 0


def _cmd_companies_list(args) -> int:
    companies = list_companies(connect(args.db), active_only=args.active)
    if args.json:
        print(json.dumps(companies, indent=2, ensure_ascii=False))
        return 0
    rows = []
    backed = any(c["backed_by"] for c in companies)
    for c in companies:
        boards = c["boards"]
        successes = [b["last_success_at"] for b in boards if b["last_success_at"]]
        errors = [f"{b['source']}/{b['ref']}: {b['last_error']}" for b in boards if b["last_error"]]
        rows.append([
            c["name"], "yes" if c["active"] else "no",
            ", ".join(f"{b['source']}/{b['ref']}" for b in boards) or "-",
            c["open_roles"] if boards else "",
            max(successes)[:16].replace("T", " ") if successes else "",
            "; ".join(errors), ", ".join(c["tags"]),
            *([", ".join(c["backed_by"])] if backed else [])])
    print(format_table(
        ["Company", "Active", "Boards", "Open", "Last success (UTC)", "Last error", "Tags",
         *(["Backed by"] if backed else [])],
        rows, {"Last error": 50, "Backed by": 40}))
    mapped = sum(1 for c in companies if c["boards"])
    print(f"\n{len(companies)} companies, {mapped} mapped to a board.")
    return 0


def _cmd_companies_add(args) -> int:
    conn = connect(args.db)
    add_company(conn, args.name, website=args.website,
                tags=args.tags.split(",") if args.tags else None)
    print(f"Added {args.name.strip()}.")
    return 0


def _cmd_companies_set_website(args) -> int:
    name, old, new = set_website(connect(args.db), args.name, args.url)
    if new == old:
        print(f"{name}: website is already {new or 'empty'}.")
    else:
        print(f"{name}: website changed from {old or '(none)'} to {new or '(none)'}.")
        print("Its job board is looked for again at the next weekly re-check, or now with: "
              f'rolescout companies resolve "{name}"')
    return 0


def _cmd_companies_set_active(args) -> int:
    conn = connect(args.db)
    set_active(conn, args.name, args.active)
    name = get_company(conn, args.name)["name"]
    print(f"{name} is now {'active' if args.active else 'inactive'}.")
    return 0


def _cmd_companies_map(args) -> int:
    conn = connect(args.db)
    add_board(conn, args.name, args.source, args.ref)
    print(f"{get_company(conn, args.name)['name']} -> {args.source}/{board_ref(args.source, args.ref)}")
    return 0


def _cmd_companies_unmap(args) -> int:
    conn = connect(args.db)
    closed = remove_board(conn, args.name, args.source, args.ref)
    print(f"Removed {args.source}/{board_ref(args.source, args.ref)} from "
          f"{get_company(conn, args.name)['name']}; "
          f"{closed} open role{'' if closed == 1 else 's'} closed.")
    return 0


# --- resolve and report -----------------------------------------------------

def _print_resolution(res: Resolution, dry_run: bool) -> None:
    verb = "would save" if dry_run else "saved"
    if res.outcome == "mapped":
        boards = ", ".join(f"{c.source}/{c.ref}" for c in res.to_save)
        line = f"{verb} {boards}"
    elif res.outcome == "needs_review":
        line = (f"uses {res.job_system}, board name not found" if res.job_system and not res.candidates
                else "needs review")
    elif res.outcome == "unsupported":
        line = f"unsupported: {res.job_system} ({res.job_system_url})"
    elif res.over_budget:
        line = "stopped at its time budget, left unidentified"
    else:
        line = "unresolved"
    print(f"  {res.company} ... {line}", flush=True)


def _candidate_rows(results: list[Resolution], wanted) -> list[list]:
    return [[r.company, f"{c.source}/{c.ref}", c.kind, c.roles, c.detail]
            for r in results for c in r.candidates if wanted(r, c)]


def _print_resolve_summary(results: list[Resolution], dry_run: bool) -> None:
    verb = "Would save" if dry_run else "Saved"
    headers = ["Company", "Board", "Found by", "Roles", "Evidence"]
    widths = {"Evidence": 130}
    saved = _candidate_rows(results, lambda r, c: c in r.to_save and (c.saved or dry_run))
    if saved:
        print(f"\n{verb} ({len(saved)}):")
        print(format_table(headers, saved, widths))
    review = _candidate_rows(results, lambda r, c: c.status == "unconfirmed")
    if review:
        print(f"\nNot saved, needs your confirmation ({len(review)}) "
              "- accept one with: rolescout companies map NAME SOURCE REF")
        print(format_table(headers, review, widths))
    rejected = _candidate_rows(results, lambda r, c: c.status == "rejected")
    if rejected:
        print(f"\nRejected, a different company owns the slug ({len(rejected)}):")
        print(format_table(headers, rejected, widths))
    boardless = [[r.company, r.job_system, r.job_system_url] for r in results
                 if r.outcome == "needs_review" and r.job_system and not r.candidates]
    if boardless:
        print(f"\nOn a supported system, but the board name was not found ({len(boardless)}) "
              "- map by hand: rolescout companies map NAME SOURCE BOARD")
        print(format_table(["Company", "Job system", "Seen on"], boardless, {"Seen on": 100}))
    unsupported = [[r.company, r.job_system, r.job_system_url] for r in results
                   if r.outcome == "unsupported"]
    if unsupported:
        print(f"\nJob system detected but not supported ({len(unsupported)}):")
        print(format_table(["Company", "Job system", "URL"], unsupported, {"URL": 100}))
    unresolved = [[r.company, "; ".join(r.notes) or "no careers page or board found"]
                  for r in results if r.outcome == "unresolved"]
    if unresolved:
        print(f"\nNot identified ({len(unresolved)}) - if you find the job system yourself: "
              "rolescout companies set-system NAME SYSTEM URL")
        print(format_table(["Company", "What happened"], unresolved, {"What happened": 110}))
    counts = {o: sum(r.outcome == o for r in results)
              for o in ("mapped", "needs_review", "unsupported", "unresolved")}
    print(f"\n{len(results)} companies: {counts['mapped']} mapped, {counts['needs_review']} need review, "
          f"{counts['unsupported']} on an unsupported system, {counts['unresolved']} unresolved. "
          f"{sum(r.requests for r in results)} requests.")


def _cmd_companies_resolve(args) -> int:
    conn = connect(args.db)
    recheck = getattr(args, "recheck", False)
    if not args.json:
        print(f"Database: {resolve_db_path(args.db)}" + ("  (dry run: nothing is saved)" if args.dry_run else ""))
    failing = failing_boards(conn) if recheck else []
    if failing and not args.json:
        print("Also re-resolving companies whose board has failed three or more runs in a row: "
              + ", ".join(sorted({b["company"] for b in failing})))
    left: list[str] = []
    results = resolve_companies(
        conn, names=None if recheck else args.names, failing=failing, dry_run=args.dry_run,
        delay=args.delay, on_company=None if args.json else lambda r: _print_resolution(r, args.dry_run),
        time_limit=args.max_minutes * 60 if args.max_minutes else None, not_checked=left)
    if recheck and not args.dry_run:
        mark_rechecked(conn)
    if args.json:
        print(json.dumps([{**vars(r), "candidates": [vars(c) for c in r.candidates]}
                          for r in results], indent=2, ensure_ascii=False))
        return 0
    if not results:
        print("Nothing to resolve: every active company has a board or a recorded job system.")
        return 0
    _print_resolve_summary(results, args.dry_run)
    if left:
        print(f"Stopped at the {args.max_minutes:g}-minute limit; not checked ({len(left)}): {', '.join(left)}")
    return 0


def _cmd_companies_set_system(args) -> int:
    conn = connect(args.db)
    set_job_system(conn, args.name, args.system, args.url, args.note or "")
    row = conn.execute(
        "SELECT c.name, r.job_system FROM company_resolutions r JOIN companies c "
        "ON c.id = r.company_id WHERE c.name = ?", (args.name.strip(),)).fetchone()
    print(f"{row['name']}: job system recorded as {row['job_system']} ({args.url.strip()}). "
          "Automatic detection will not overwrite it.")
    return 0


def _cmd_companies_clear_system(args) -> int:
    conn = connect(args.db)
    name = get_company(conn, args.name)["name"]
    if clear_job_system(conn, args.name):
        print(f"{name}: hand-recorded job system removed; the next resolve will look again.")
    else:
        print(f"{name} has no hand-recorded job system.")
    return 0


# --- gaps and connector briefs ----------------------------------------------

def _print_gaps(report: dict) -> None:
    uncovered = report["active_companies"] - report["covered"]
    print(f"Coverage gaps: {summary_line(report)} "
          f"({uncovered} of {report['active_companies']} active companies not covered)")

    def system_rows(groups):
        return [[g["system"] if i == 0 else "", len(g["companies"]) if i == 0 else "",
                 g["why"] if i == 0 else "", m["company"],
                 (m["url"] or "") + ("  (by hand)" if m["manual"] else "")]
                for g in groups for i, m in enumerate(g["companies"])]
    headers = ["Job system", "Count", "Why", "Company", "URL"]
    candidates = [g for g in report["systems"] if g["build_candidate"]]
    others = [g for g in report["systems"] if not g["build_candidate"]]
    print("\nBuild candidates (3 or more companies) "
          "- details: rolescout brief SYSTEM")
    print(format_table(headers, system_rows(candidates), {"URL": 80}) if candidates else "  (none)")
    print("\nOther unsupported job systems:")
    print(format_table(headers, system_rows(others), {"URL": 80}) if others else "  (none)")

    print("\nOn a supported system, not mapped yet - rolescout companies map NAME SOURCE BOARD")
    rows = []
    for e in report["needs_mapping"]:
        boards = "; ".join(f"{c['source']}/{c['ref']}: {c['detail']}" for c in e["candidates"])
        rows.append([e["company"], e["system"] or "",
                     boards or (e["notes"][-1] if e["notes"] else "")])
    print(format_table(["Company", "System", "Detail"], rows, {"Detail": 110}) if rows else "  (none)")

    print("\nNeeds identification - rolescout companies set-system NAME SYSTEM URL")
    rows = [[e["company"], e["careers_url"] or e["website"] or "", e["reason"]]
            for e in report["unidentified"]]
    print(format_table(["Company", "Careers page", "Why"], rows, {"Why": 80}) if rows else "  (none)")


def _cmd_gaps(args) -> int:
    report = gaps_report(connect(args.db))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_gaps(report)
    return 0


def _cmd_brief(args) -> int:
    print(connector_brief(connect(args.db), args.system))
    return 0


def _cmd_companies_report(args) -> int:
    report = mapping_report(connect(args.db))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    mapped_companies = {m["company"] for boards in report["mapped"].values() for m in boards}
    unsupported = sum(len(v) for v in report["unsupported"].values())
    print(f"{report['total']} active companies: {len(mapped_companies)} mapped, "
          f"{unsupported} on an unsupported job system, {len(report['unresolved'])} unresolved.")

    print("\nMapped, by source:")
    rows = [[source, len(boards), ", ".join(m["company"] for m in boards)]
            for source, boards in sorted(report["mapped"].items(), key=lambda kv: -len(kv[1]))]
    print(format_table(["Source", "Boards", "Companies"], rows, {"Companies": 110}) if rows else "  (none)")

    print("\nDetected but unsupported, by job system:")
    rows = [[system if i == 0 else "", len(found) if i == 0 else "", f["company"], f["url"]]
            for system, found in sorted(report["unsupported"].items(), key=lambda kv: (-len(kv[1]), kv[0]))
            for i, f in enumerate(found)]
    print(format_table(["Job system", "Count", "Company", "URL"], rows, {"URL": 90}) if rows else "  (none)")

    print("\nUnresolved:")
    rows = []
    for u in report["unresolved"]:
        pending = [c for c in u["candidates"] if c["status"] == "unconfirmed"]
        detail = ("; ".join(f"{c['source']}/{c['ref']}: {c['detail']}" for c in pending)
                  or (u["notes"][-1] if u["reason"] == "needs review" and u["notes"] else u["reason"]))
        rows.append([u["company"], u["reason"].split(";")[0][:40], detail])
    print(format_table(["Company", "Status", "Detail"], rows, {"Detail": 100}) if rows else "  (none)")
    return 0


# --- scout ------------------------------------------------------------------

def _cost(requests: int | None, seconds: float) -> str:
    """", 44 requests, 51s": what a board's fetch cost."""
    if requests is None:
        return f", {seconds:.0f}s"
    return f", {requests} request{'' if requests == 1 else 's'}, {seconds:.0f}s"


def _print_progress(result: BoardResult) -> None:
    if result.status == "ok":
        detail = f"{result.roles_fetched} roles" + (" (baseline)" if result.baseline else "")
        if result.roles_reported and result.roles_seen != result.roles_reported:
            detail += f" ({result.roles_seen} of {result.roles_reported} listed)"
        if not result.complete:
            detail += ", partial"
    else:
        detail = f"FAILED: {result.error}"
    print(f"  {result.company} ({result.source}/{result.ref}) ... {detail}"
          f"{_cost(result.requests, result.duration_ms / 1000)}", flush=True)
    if result.note:
        print(f"      {result.note}", flush=True)


def _description_rule(args, quiet: bool = False):
    """Which titles are worth a description request of their own, from the
    ranking config. None when there is no usable config."""
    path = _ranking_path(args)
    try:
        return ranking.load_config(path).wants_title
    except ranking.ConfigError as exc:
        if not quiet:
            print(f"Note: {exc}. Where a description costs a request of its own (Workday, Rippling), "
                  "it is fetched for saved and applied roles only.")
        return None


def _scout_once(conn, args, *, as_json: bool = False):
    """Run one scout and print its summary. Returns the summary."""
    by_request = sorted(name for name, cls in ADAPTERS.items() if cls.uses_context)
    uses_rule = conn.execute(
        "SELECT 1 FROM boards b JOIN companies c ON c.id = b.company_id "
        f"WHERE c.active = 1 AND b.source IN ({', '.join('?' * len(by_request))}) LIMIT 1",
        by_request).fetchone() is not None
    summary = run_scout(
        conn, delay=args.delay, on_board=None if as_json else _print_progress,
        wants_description=_description_rule(args, quiet=as_json) if uses_rule else None)

    if as_json:
        print(json.dumps({
            "run_id": summary.run_id, "started_at": summary.started_at,
            "finished_at": summary.finished_at, "status": summary.status,
            "boards_ok": summary.ok, "boards_failed": summary.failed,
            "unmapped_skipped": summary.unmapped,
            "boards": [vars(b) for b in summary.boards],
            "companies": [vars(s) for s in summary.snapshots],
        }, indent=2, ensure_ascii=False))
        _rebuild_after_scout(conn, args, summary.run_id, quiet=True)
        return summary
    print(f"\nRun {summary.run_id} {summary.status}: {summary.started_at} -> {summary.finished_at}")
    if summary.boards:
        rows = []
        for snap in summary.snapshots:
            boards = [b for b in summary.boards if b.company == snap.company]
            failed = [b for b in boards if b.status != "ok"]
            status = "ok" if not failed else ("error" if len(failed) == len(boards) else "partial")
            rows.append([
                snap.company, ", ".join(f"{b.source}/{b.ref}" for b in boards), status,
                sum(b.roles_fetched or 0 for b in boards), snap.open_roles,
                "baseline" if snap.baseline and not snap.opened else snap.opened, snap.closed,
                sum(b.requests or 0 for b in boards),
                f"{sum(b.duration_ms for b in boards) / 1000:.0f}s",
                "; ".join(b.error or "" for b in failed)])
        rows.append(["TOTAL", f"{len(summary.boards)} boards",
                     f"{summary.ok} ok, {summary.failed} failed",
                     sum(b.roles_fetched or 0 for b in summary.boards),
                     summary.total("open_roles"), summary.total("opened"),
                     summary.total("closed"),
                     sum(b.requests or 0 for b in summary.boards),
                     f"{sum(b.duration_ms for b in summary.boards) / 1000:.0f}s", ""])
        print(format_table(
            ["Company", "Boards", "Status", "Fetched", "Open", "Opened", "Closed", "Requests",
             "Time", "Error"],
            rows, {"Error": 60}))
    else:
        print("No active company is mapped to a board. Use: rolescout companies resolve "
              "(or: rolescout companies map NAME SOURCE REF)")
    if summary.unmapped:
        print(f"\nSkipped {summary.unmapped} active companies with no board mapping.")
    _rebuild_after_scout(conn, args, summary.run_id)
    return summary


# --- shortlist --------------------------------------------------------------

def _ranking_path(args) -> Path:
    return ranking.config_path(resolve_db_path(args.db), getattr(args, "config", None))


def _built_line(result: dict) -> str:
    line = (f"Shortlist rebuilt: {result['listed']} roles "
            f"(of {result['eligible']} that qualify)")
    if result["held_back"]:
        line += f", {result['held_back']} held back by the per-company cap"
    if result["previous_date"]:
        line += f", {result['new']} new since {result['previous_date']}"
    return line + ". See: rolescout shortlist"


def _rebuild_after_scout(conn, args, run_id: int, quiet: bool = False) -> None:
    """Rebuild the shortlist at the end of a scout. Never fails the scout."""
    path = _ranking_path(args)
    try:
        if not path.exists():
            if not quiet:
                print(f"\nNo shortlist: there is no ranking config at {path}. "
                      "Create one with: rolescout shortlist init")
            return
        result = ranking.build_shortlist(conn, ranking.load_config(path), run_id=run_id)
        if not quiet:
            print("\n" + _built_line(result))
    except Exception as exc:
        if not quiet:
            print(f"\nShortlist not rebuilt, the previous one is kept: {exc}")


def _silent_rebuild(conn, args) -> None:
    """After a mark changes: refresh today's list if there is a config. Best effort."""
    try:
        path = _ranking_path(args)
        if path.exists():
            ranking.build_shortlist(conn, ranking.load_config(path))
    except Exception:
        pass


def _cmd_shortlist_init(args) -> int:
    path = _ranking_path(args)
    if path.exists() and not args.force:
        print(f"error: {path} already exists. Edit it, or pass --force to replace it "
              "(your changes would be lost).", file=sys.stderr)
        return 1
    print(_write_ranking_starter(path, args.filters))
    print("Next: edit it if you like, then run: rolescout shortlist rebuild")
    _warn_inside_repo(path)
    return 0


def _write_private(path: Path, text: str) -> None:
    """Write a personal config file, readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _warn_inside_repo(path: Path) -> None:
    repo = _inside_git_repo(path)
    if repo:
        print(f"warning: {path} is inside the git repository at {repo}. It is personal data; "
              "keep it out of commits.", file=sys.stderr)


def _write_ranking_starter(path: Path, filters_arg: str | None = None) -> str:
    """Write the starter ranking config; the line that says what was written."""
    titles, seniority, seeded = [], [], ""
    if filters_arg:
        filters = Path(filters_arg).expanduser()
        if not filters.exists():
            raise FileNotFoundError(f"no such file: {filters}")
        titles, seniority = ranking.read_filters_csv(filters)
        seeded = filters.name
    text = ranking.starter_config(titles, seniority, seeded)
    ranking.parse_config(text)  # a starter that does not load would be a bug
    _write_private(path, text)
    if seeded:
        return (f"Wrote {path}: {len(titles)} title phrases and {len(seniority)} seniority words "
                f"from {filters_arg}.")
    return (f"Wrote {path}: the starter ranking for product and engineering roles "
            f"({len(ranking.STARTER_TITLES)} title phrases, no location preference).")


def _cmd_shortlist_rebuild(args) -> int:
    conn = connect(args.db)
    cfg = ranking.load_config(_ranking_path(args))
    print(_built_line(ranking.build_shortlist(conn, cfg)))
    return 0


def _flags(entry: dict, new_ids: set[int]) -> str:
    return " ".join(flag for flag, on in (("saved", entry["mark"] == "saved"),
                                           ("new", entry["id"] in new_ids)) if on)


def _filter(conn, args) -> filters.RoleFilter:
    return filters.build_filter(conn, stage=args.stage, sector=args.sector, hq=args.hq,
                                location=args.location, employees=args.employees, vc=args.vc)


def _cmd_shortlist_filtered(conn, args, only: filters.RoleFilter) -> int:
    """Rank every qualifying role that passes the filter, as of now."""
    cfg = ranking.load_config(_ranking_path(args))
    result = ranking.filtered_entries(conn, cfg, only)
    entries = result["entries"]
    total = len(entries)
    if args.top:
        entries = entries[:args.top]
    if args.json:
        print(json.dumps({"filter": only.describe(), "matching": total, "qualifying_overall": result["qualifying"],
                          "entries": entries}, indent=2, ensure_ascii=False))
        return 0
    off_list = sum(1 for e in result["entries"] if e["list_rank"] is None)
    print(f"Filtered ranking ({only.describe()}): {total} of the {result['qualifying']} roles that qualify, "
          f"ranked now; {off_list} of them are not on today's shortlist")
    if any(e["postings"] > 1 for e in entries):
        print("A job posted once per location is one entry: its best-scoring posting, with the other "
              "locations counted in \"+N more\".")
    if not entries:
        print("\nNo roles to show.")
        return 0
    print()
    widths = {"Title": 52, "Location": 26, "Company": 18, "Backed by": 28}
    backed = ["Backed by"] if any(e["backed_by"] for e in entries) else []

    def row(e: dict, with_company: bool) -> list:
        return [e["rank"], e["id"], f"{e['score']:g}", *([e["company"]] if with_company else []), e["title"],
                e["location_shown"], *([", ".join(e["backed_by"])] if backed else []),
                "-" if e["list_rank"] is None else e["list_rank"], "saved" if e["mark"] == "saved" else "", e["url"]]
    if args.by_company:
        groups: dict[str, list[dict]] = {}
        for e in entries:
            groups.setdefault(e["company"], []).append(e)
        for company, members in groups.items():
            print(f"{company}: {len(members)} role{'' if len(members) == 1 else 's'}, best rank {members[0]['rank']}")
            print(format_table(["Rank", "ID", "Score", "Title", "Location", *backed, "On list", "Flags", "URL"],
                               [row(e, False) for e in members], widths))
            print()
    else:
        print(format_table(["Rank", "ID", "Score", "Company", "Title", "Location", *backed, "On list", "Flags", "URL"],
                           [row(e, True) for e in entries], widths))
        print()
    print("\"On list\" is the role's rank on today's shortlist; - means it is not on it.")
    return 0


def _cmd_shortlist(args) -> int:
    conn = connect(args.db)
    only = _filter(conn, args)
    if only:
        return _cmd_shortlist_filtered(conn, args, only)
    current = ranking.latest_shortlist(conn)
    path = _ranking_path(args)
    if current is None:
        print("No shortlist yet. " + (
            "Run: rolescout shortlist rebuild" if path.exists()
            else "Create a ranking config with: rolescout shortlist init"))
        return 0
    try:
        previous, new_ids = ranking.new_since(conn, current["id"], args.since)
    except ValueError:
        print(f"error: --since must be a date like 2026-01-31, got {args.since!r}", file=sys.stderr)
        return 1
    try:
        shown_with = ranking.load_config(path) if path.exists() else None
    except ConfigError:
        shown_with = None
    entries = ranking.shortlist_entries(conn, current["id"], shown_with)
    total = len(entries)
    if args.new:
        entries = [e for e in entries if e["id"] in new_ids]
    if args.top:
        entries = entries[:args.top]
    for e in entries:
        e["new"] = e["id"] in new_ids and previous is not None

    stale = ""
    try:
        if path.exists() and ranking.load_config(path).digest != current["config_hash"]:
            stale = "The ranking config has changed since this list was built; run: rolescout shortlist rebuild"
    except ConfigError as exc:
        stale = f"The ranking config cannot be read ({exc}); this is the last list built."

    if args.json:
        print(json.dumps({
            "list_date": current["list_date"], "built_at": current["built_at"],
            "eligible": current["eligible"], "size": total,
            "compared_with": previous["list_date"] if previous else None,
            "config_changed_since_build": bool(stale), "held_back_by_cap": len(ranking.held_back(conn, current["id"])),
            "entries": entries,
        }, indent=2, ensure_ascii=False))
        return 0

    compared = f"new since {previous['list_date']}: {len(new_ids)}" if previous else "no earlier list to compare with"
    held = ranking.held_back(conn, current["id"])
    held_by_company: dict[str, int] = {}
    for h in held:
        held_by_company[h["company"]] = held_by_company.get(h["company"], 0) + 1
    print(f"Shortlist for {current['list_date']}: {total} roles of {current['eligible']} that qualify; {compared}"
          + (f"; {len(held)} held back by the per-company cap" if held else ""))
    if any(e["postings"] > 1 for e in entries):
        print("A job posted once per location is one entry: its best-scoring posting, with the other "
              "locations counted in \"+N more\". rolescout why ID lists them.")
    if stale:
        print(stale)
    if args.new and previous is None:
        print("\nNothing to show: there is no earlier list, so nothing can be new yet.")
        return 0
    if not entries:
        print("\nNo roles to show.")
        return 0
    print()
    widths = {"Title": 52, "Location": 26, "Company": 18, "Backed by": 28}
    # The column appears once any role shown is at a company a portfolio board lists.
    backed = ["Backed by"] if any(e["backed_by"] for e in entries) else []

    def backers(e: dict) -> list[str]:
        return [", ".join(e["backed_by"])] if backed else []
    if args.by_company:
        groups: dict[str, list[dict]] = {}
        for e in entries:
            groups.setdefault(e["company"], []).append(e)
        # Entries arrive in rank order, so groups are already ordered by their best role.
        for company, members in groups.items():
            more = held_by_company.get(company, 0)
            print(f"{company}: {len(members)} role{'' if len(members) == 1 else 's'}, "
                  f"best rank {members[0]['rank']}"
                  + (f"; {more} more held back by the per-company cap" if more else ""))
            print(format_table(
                ["Rank", "ID", "Score", "Title", "Location", *backed, "Flags", "URL"],
                [[e["rank"], e["id"], f"{e['score']:g}", e["title"], e["location_shown"], *backers(e),
                  _flags(e, new_ids), e["url"]] for e in members], widths))
            print()
    else:
        print(format_table(
            ["Rank", "ID", "Score", "Company", "Title", "Location", *backed, "Flags", "URL"],
            [[e["rank"], e["id"], f"{e['score']:g}", e["company"], e["title"],
              e["location_shown"], *backers(e), _flags(e, new_ids), e["url"]] for e in entries], widths))
        print()
    print("Why a role ranks where it does: rolescout why ID.  "
          "Mark one: rolescout mark ID dismissed|saved|applied")
    return 0


def _cmd_mark(args) -> int:
    conn = connect(args.db)
    role_id = ranking.parse_role_id(args.id)
    role = ranking.mark_role(conn, role_id, args.status, args.note or "")
    _silent_rebuild(conn, args)
    effect = {"dismissed": "it will not be shortlisted again",
              "applied": "it will not be shortlisted again, and its description is kept after it closes",
              "saved": "it stays on the shortlist, flagged, and its description is kept after it closes"}
    print(f"Marked {args.status}: {role['company']}, {role['title']} (ID {role_id}); {effect[args.status]}.")
    others = [] if role["closed_at"] else ranking.other_open_postings(conn, role)
    if others and args.status in ranking.HIDDEN_MARKS:
        print(f"The same job is posted {len(others)} more time{'' if len(others) == 1 else 's'} "
              f"(ID {', '.join(str(o['id']) for o in others)}); the mark covers the job, so "
              f"{'that stays' if len(others) == 1 else 'those stay'} off the shortlist too.")
    if role["closed_at"] and args.status in ranking.KEEP_DESCRIPTION_MARKS:
        print("Note: this role had already closed, so its description was already deleted.")
    return 0


def _cmd_unmark(args) -> int:
    conn = connect(args.db)
    role_id = ranking.parse_role_id(args.id)
    was = ranking.unmark_role(conn, role_id)
    role = ranking.get_role(conn, role_id)
    _silent_rebuild(conn, args)
    if was:
        print(f"Removed the {was} mark from {role['company']}, {role['title']} (ID {role_id}).")
    else:
        print(f"{role['company']}, {role['title']} (ID {role_id}) had no mark.")
    return 0


def _cmd_marks(args) -> int:
    marks = ranking.list_marks(connect(args.db), args.status)
    if args.json:
        print(json.dumps(marks, indent=2, ensure_ascii=False))
        return 0
    rows = [[m["id"], m["status"], m["marked_at"][:10], m["company"], m["title"],
             "closed " + m["closed_at"][:10] if m["closed_at"] else "open",
             "yes" if m["has_description"] else "no", m["note"]] for m in marks]
    print(format_table(["ID", "Mark", "Marked", "Company", "Title", "Role", "Description kept", "Note"],
                       rows, {"Title": 50, "Note": 50}))
    print(f"\n{len(marks)} marked role{'' if len(marks) == 1 else 's'}.")
    return 0


def _cmd_why(args) -> int:
    conn = connect(args.db)
    info = ranking.explain(conn, ranking.parse_role_id(args.id), ranking.load_config(_ranking_path(args)))
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
        return 0
    role = info["role"]
    print(f"{role['title']}  (ID {role['id']})")
    print(f"{role['company']} | {role['location'] or 'no location given'} | {role['url']}")
    if role["backed_by"]:
        print(f"Backed by: {', '.join(role['backed_by'])}")
    if role["mark"]:
        print(f"Marked {role['mark']}" + (f": {role['mark_note']}" if role["mark_note"] else ""))
    print()
    under = info["listed_under"]
    if info["rank"]:
        print(f"Rank {info['rank']} of {info['list_size']} on the shortlist for {info['list_date']}.")
        if under:
            print(f"The entry is this job's best-scoring posting, ID {under['id']} "
                  f"({under['location'] or 'no location given'}, score {under['score']:g}); "
                  "this posting is listed with it.")
    else:
        print(f"Not on the shortlist: {info['why_not']}.")
    if info["other_postings"]:
        print("Also posted for: " + "; ".join(
            f"{o['location'] or 'no location given'} (ID {o['id']}, score {o['score']:g})"
            for o in info["other_postings"]))
    print(f"\nScore {info['score']:g}, from:")
    for line in info["detail"]:
        print(f"  {line}")
    if info["rank"] and info["listed_score"] != info["score"]:
        print(f"\nThe list was built with a score of {info['listed_score']:g}; the config or the "
              "date has changed since. Run: rolescout shortlist rebuild")
    elif info["config_changed"]:
        print("\nThe ranking config has changed since the list was built; "
              "run: rolescout shortlist rebuild")
    return 0


# --- MCP server -------------------------------------------------------------

def _cmd_serve(args) -> int:
    """Serve the database to MCP clients. Output goes to stderr: on stdio, stdout is the protocol."""
    from rolescout import mcp_server

    def say(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    db_path = resolve_db_path(args.db)
    if not db_path.exists():
        say(f"error: no database at {db_path}. Run a scout first, or pass --db.")
        return 1
    try:
        server = mcp_server.build_server(db_path, args.config, read_only=args.read_only)
    except RuntimeError as exc:
        say(f"error: {exc}")
        return 1
    mode = "read-only" if args.read_only else "read-write"
    if not args.http:
        if args.host or args.port or args.allow_host or args.token_file:
            say("error: --host, --port, --allow-host and --token-file only apply with --http.")
            return 1
        say(f"rolescout MCP server on stdio, {mode}, database {db_path}")
        mcp_server.serve_stdio(server)
        return 0

    host, port = args.host or "127.0.0.1", args.port or mcp_server.DEFAULT_PORT
    try:
        token = mcp_server._token_from_env_or_file(args.token_file)
    except OSError as exc:
        say(f"error: cannot read the token file: {exc}")
        return 1
    if not token:
        say(f"error: HTTP needs a token. Set ${mcp_server.TOKEN_ENV_VAR} or pass --token-file PATH. "
            "Create one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")
        return 1
    if len(token) < mcp_server.MIN_TOKEN_CHARS:
        say(f"error: the token must be at least {mcp_server.MIN_TOKEN_CHARS} characters.")
        return 1
    say(f"rolescout MCP server on http://{host}:{port}/mcp, {mode}, database {db_path}")
    say("Accepting Host: " + ", ".join(mcp_server.allowed_hosts(host, port, args.allow_host)))
    if host in ("0.0.0.0", "::"):
        say("warning: listening on every network interface. This server is meant for localhost "
            "or a private network such as Tailscale; bind to that address instead.")
    mcp_server.serve_http(server, token, host=host, port=port, allow_hosts=args.allow_host)
    return 0


def _cmd_scout(args) -> int:
    conn = connect(args.db)
    if not args.json:
        print(f"Database: {resolve_db_path(args.db)}")
    try:
        summary = _scout_once(conn, args, as_json=args.json)
    except ScoutAlreadyRunning as exc:
        # Not an error: the other scout is doing the work.
        print(f"Not starting: {exc}.")
        return 0
    # Non-zero only when nothing could be fetched at all (e.g. no network).
    return 1 if summary.boards and summary.ok == 0 else 0


# --- unattended running: daily, backup, status, schedule --------------------

def _human_bytes(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB" if n >= 100_000 else f"{n / 1000:.1f} KB"


def _print_backup(conn, directory: str, keep: int) -> None:
    target, removed = backup_database(conn, directory, keep=keep)
    print(f"Backup written: {target} ({_human_bytes(target.stat().st_size)})"
          + (f"; removed {len(removed)} older backup{'' if len(removed) == 1 else 's'}" if removed else ""))


def _cmd_backup(args) -> int:
    _print_backup(connect(args.db), args.directory, args.keep)
    return 0


def _weekly_recheck(conn, args) -> None:
    """Re-resolve unmapped companies and owners of failing boards. Never fails the daily run."""
    try:
        failing = failing_boards(conn)
        print("\nWeekly coverage re-check"
              + (": includes boards failing three or more runs in a row ("
                 + ", ".join(f"{b['company']} {b['source']}/{b['ref']}" for b in failing) + ")"
                 if failing else ""))
        left: list[str] = []
        results = resolve_companies(conn, failing=failing, delay=args.delay,
                                    on_company=lambda r: _print_resolution(r, False),
                                    time_limit=args.recheck_minutes * 60, not_checked=left)
        # Marked even when cut short: the weekly cost stays bounded, and the
        # companies left over are first in line next week.
        mark_rechecked(conn)
        if results:
            _print_resolve_summary(results, False)
        else:
            print("Nothing to re-check.")
        if left:
            print(f"Re-check stopped at its {args.recheck_minutes:g}-minute limit; not checked this "
                  f"week, first in line next week ({len(left)}): {', '.join(left)}")
        for board in failing:
            added = [c for r in results if r.company == board["company"] for c in r.candidates if c.saved]
            if added:
                print(f"{board['company']}: found {', '.join(f'{c.source}/{c.ref}' for c in added)}. "
                      f"The old board {board['source']}/{board['ref']} is still mapped and failing; "
                      f"remove it with: rolescout companies unmap \"{board['company']}\" "
                      f"{board['source']} {board['ref']}")
        print(f"Coverage gaps: {summary_line(gaps_report(conn))}")
    except Exception as exc:
        print(f"Coverage re-check failed, will try again tomorrow: {type(exc).__name__}: {exc}")


# --- portfolio boards and candidates ----------------------------------------

def _candidates_config(args) -> Path:
    return cands.config_path(resolve_db_path(args.db), getattr(args, "config", None))


def _cmd_portfolio_add(args) -> int:
    conn = connect(args.db)
    board = portfolio.add_board(conn, args.url, args.name or "", args.vc or "")
    owner = conn.execute("SELECT name FROM vcs WHERE id = ?", (board["vc_id"],)).fetchone()
    print(f"Registered {board['url']}" + (f" under {owner['name']}" if owner else "")
          + ". Read its companies with: rolescout portfolio harvest")
    return 0


def _cmd_portfolio_list(args) -> int:
    boards = portfolio.list_boards(connect(args.db))
    if args.json:
        print(json.dumps(boards, indent=2, ensure_ascii=False))
        return 0
    rows = [[b["id"], b["name"] or "-", b["url"], b["platform"] or "-",
             "" if b["companies_listed"] is None else b["companies_listed"], b["candidates"],
             (b["last_harvest_at"] or "never")[:16].replace("T", " "), b["last_error"] or ""] for b in boards]
    print(format_table(["ID", "Name", "URL", "Platform", "Listed", "Candidates", "Last harvest (UTC)", "Problem"],
                       rows, {"Problem": 60}))
    print(f"\n{len(boards)} board{'' if len(boards) == 1 else 's'}.")
    return 0


def _cmd_portfolio_remove(args) -> int:
    url, orphans = portfolio.remove_board(connect(args.db), args.board)
    print(f"Removed {url}. Its candidates are kept"
          + (f"; {orphans} are now listed on no board." if orphans else "."))
    return 0


def _funnel_lines(f: dict) -> list[str]:
    return [
        f"Listed on your boards:      {f['listed']:>6}",
        f"  already tracked:          {f['already_tracked']:>6}",
        f"  dismissed:                {f['dismissed']:>6}",
        f"Considered:                 {f['considered']:>6}",
        f"  passed size:              {f['passed_size']:>6}",
        f"  passed stage:             {f['passed_stage']:>6}",
        f"  passed sector:            {f['passed_sector']:>6}",
        f"  passed open-role rule:    {f['passed_roles']:>6}",
        f"Eligible:                   {f['eligible']:>6}",
        f"Needs review:               {f['needs_review']:>6}",
        f"Awaiting the role check:    {f['awaiting_role_check']:>6}",
        f"Rejected:                   {f['rejected']:>6}",
    ]


def _print_harvest(summary) -> None:
    rows = [[b.name or "-", b.url, b.listed, b.read, b.fresh, b.unreadable, b.no_domain, b.role_checks,
             b.error or ("" if b.finished else "stopped at the time limit")] for b in summary.boards]
    print(format_table(["Board", "URL", "Listed", "Read", "Fresh", "Unreadable", "No domain",
                        "Role checks", "Note"], rows, {"Note": 60}))
    print(f"\n{summary.requests} requests in {summary.seconds:.0f} seconds"
          + ("; stopped at the time limit, the next harvest continues from here." if summary.stopped_early else "."))


def _run_harvest(conn, args, minutes: float | None, boards: list[str] | None = None):
    cfg = cands.load_config(_candidates_config(args))
    summary = portfolio.harvest(
        conn, cfg, boards=boards, delay=args.delay, time_limit=minutes * 60 if minutes else None,
        on_board=lambda b: print(f"  {b.name or b.url}: " + (b.error or f"{b.listed} companies listed, "
                                 f"{b.read} read, {b.fresh} checked recently"), flush=True))
    _print_harvest(summary)
    print()
    print("\n".join(_funnel_lines(cands.funnel(conn))))
    done, failed = cands.auto_activate(conn, cfg)
    if done or failed:
        print(f"\nActivated automatically (auto_activate is on): {len(done)}")
        for d in done:
            print(f"  {d['name']} ({d['domain']}), tagged {', '.join(d['tags']) or 'with no board'}")
        for message in failed:
            print(f"  Not activated: {message}")
    summary.activated = len(done)
    portfolio.record_harvest(conn, auto_activate=cfg.auto_activate,
                             company_ids=[d["company_id"] for d in done])
    if done:
        _resolve_activated(conn, args, [d["name"] for d in done],
                           minutes * 60 - summary.seconds if minutes else None)
    return summary


def _resolve_activated(conn, args, names: list[str], seconds_left: float | None) -> None:
    """Look for the job boards of companies a harvest just activated, in what
    is left of the harvest's time limit. Whatever is not reached stays
    unmapped and is picked up by the weekly re-check. Never fails the harvest."""
    try:
        left: list[str] = []
        if seconds_left is not None and seconds_left <= 0:
            left = list(names)
            results = []
        else:
            print(f"\nFinding the job boards of the {len(names)} just activated"
                  + (f" ({seconds_left / 60:.0f} minutes left of the harvest's limit)"
                     if seconds_left is not None else ""))
            results = resolve_companies(conn, names=names, delay=args.delay,
                                        on_company=lambda r: _print_resolution(r, False),
                                        time_limit=seconds_left, not_checked=left)
        if results:
            _print_resolve_summary(results, False)
        if left:
            print(f"\nThe harvest's time limit was reached; the weekly re-check will look for the job "
                  f"boards of ({len(left)}): {', '.join(left)}")
    except Exception as exc:
        print(f"Finding their job boards failed; the weekly re-check will try: {type(exc).__name__}: {exc}")


def _cmd_portfolio_harvest(args) -> int:
    conn = connect(args.db)
    if not portfolio.list_boards(conn):
        print("No boards registered. Add one with: rolescout portfolio add URL")
        return 0
    print(f"Database: {resolve_db_path(args.db)}")
    summary = _run_harvest(conn, args, args.max_minutes, args.boards or None)
    if summary.complete and not args.boards:
        portfolio.mark_harvested(conn)
    print("\nSee them with: rolescout candidates --status " + ("tracked" if summary.activated else "eligible"))
    return 1 if summary.boards and all(b.error for b in summary.boards) else 0


def _weekly_harvest(conn, args) -> None:
    """Read the registered portfolio boards. Never fails the daily run."""
    try:
        print("\nWeekly portfolio harvest")
        summary = _run_harvest(conn, args, args.harvest_minutes)
        # A harvest cut short is not marked done: tomorrow's run carries on
        # from where it stopped, still inside the same daily limit.
        if summary.complete:
            portfolio.mark_harvested(conn)
    except Exception as exc:
        print(f"Portfolio harvest failed, will try again tomorrow: {type(exc).__name__}: {exc}")


def _cmd_candidates_init(args) -> int:
    path = _candidates_config(args)
    if path.exists() and not args.force:
        print(f"error: {path} already exists. Edit it, or pass --force to replace it "
              "(your changes would be lost).", file=sys.stderr)
        return 1
    print(_write_candidates_starter(path, args.filters))
    _warn_inside_repo(path)
    return 0


def _write_candidates_starter(path: Path, filters_arg: str | None = None) -> str:
    """Write the starter candidates filter; the line that says what was written."""
    keywords, seeded = [], ""
    if filters_arg:
        filters = Path(filters_arg).expanduser()
        if not filters.exists():
            raise FileNotFoundError(f"no such file: {filters}")
        keywords, seeded = cands.read_sector_keywords(filters), filters.name
    text = cands.starter_config(keywords, seeded)
    cands.parse_config(text)  # a starter that does not load would be a bug
    _write_private(path, text)
    if keywords:
        return f"Wrote {path}: {len(keywords)} sector keywords from {filters_arg}."
    return (f"Wrote {path}: the starter filter ({len(cands.STARTER_KEYWORDS)} broad sector keywords, "
            "every stage and size accepted).")


# --- init: the starter set ---------------------------------------------------

def _cmd_init(args) -> int:
    """Everything a new install needs before its first scout."""
    db_path = resolve_db_path(args.db)
    conn = connect(args.db)
    print(f"Database: {db_path}")
    if args.no_companies:
        print("Companies: skipped (--no-companies).")
    else:
        # Only companies that are not here yet: a company already in the
        # database keeps its website, tags, boards and active flag as they are.
        data = starter.companies()
        here = {row["name"].casefold() for row in conn.execute("SELECT name FROM companies")}
        new = [c for c in data["companies"] if c["name"].casefold() not in here]
        result = import_companies(conn, {**data, "companies": new})
        mapped = conn.execute("SELECT COUNT(DISTINCT company_id) FROM boards").fetchone()[0]
        print(f"Companies: {result['companies_added']} starter companies added, "
              f"{len(data['companies']) - len(new)} already here, {result['boards_added']} boards mapped "
              f"({mapped} companies now have a board).")
        for warning in result["warnings"]:
            print(f"  warning: {warning}")
    for path, write in ((ranking.config_path(db_path), _write_ranking_starter),
                        (cands.config_path(db_path), _write_candidates_starter)):
        if path.exists() and not args.force:
            print(f"Kept {path} (it already exists; --force replaces it).")
        else:
            print(write(path))
            _warn_inside_repo(path)
    if args.portfolio:
        added = 0
        for board in starter.portfolio_boards():
            try:
                portfolio.add_board(conn, board["url"], board.get("name") or "")
                added += 1
            except portfolio.PortfolioError:
                pass                    # already registered
        print(f"Portfolio boards: {added} example boards registered. "
              "Read them with: rolescout portfolio harvest")
    print("\nNext:\n"
          "  rolescout scout            fetch every mapped board (the first run is a baseline)\n"
          "  rolescout shortlist        the roles to look at first\n"
          f"Then make it yours: edit {ranking.config_path(db_path)}, and add or remove companies with\n"
          "rolescout companies add / deactivate. See README.md, \"Defaults and how to change them\".")
    return 0


def _cmd_candidates_apply(args) -> int:
    conn = connect(args.db)
    pending = cands.apply_filter(conn, cands.load_config(_candidates_config(args)))
    print("Filter applied (no network).")
    print("\n".join(_funnel_lines(cands.funnel(conn))))
    if pending:
        print(f"\n{len(pending)} passed the other rules and still need their open roles counted: "
              "rolescout portfolio harvest")
    return 0


def _cmd_candidates(args) -> int:
    conn = connect(args.db)
    rows = cands.list_candidates(conn, args.status)
    total = len(rows)
    if args.limit:
        rows = rows[:args.limit]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not total and not conn.execute("SELECT 1 FROM candidates LIMIT 1").fetchone():
        print("No candidates yet. Register a board (rolescout portfolio add URL), create a filter "
              "(rolescout candidates init), then: rolescout portfolio harvest")
        return 0
    table = [[r["state"], r["name"], r["domain"], r["stage"] or "", "" if r["employees"] is None else r["employees"],
              ", ".join(r["sectors"][:3]), "" if r["matching_roles"] is None else r["matching_roles"],
              len(r["boards"]),
              (f"tracked as {r['tracked_as']}" if r["state"] == "tracked"
               else r["dismiss_note"] or "dismissed" if r["state"] == "dismissed" else r["reason"])] for r in rows]
    print(format_table(["Status", "Name", "Domain", "Stage", "Employees", "Sectors", "Roles", "Boards", "Why"],
                       table, {"Name": 28, "Sectors": 40, "Why": 70}))
    shown = f"{len(rows)} of {total}" if len(rows) != total else str(total)
    print(f"\n{shown} candidate{'' if total == 1 else 's'}" + (f" with status {args.status}" if args.status else "")
          + ".  Details: rolescout candidates why NAME")
    return 0


def _cmd_candidates_why(args) -> int:
    conn = connect(args.db)
    c = cands.get_candidate(conn, args.name)
    if args.json:
        print(json.dumps(c, indent=2, ensure_ascii=False))
        return 0
    print(f"{c['name']}  ({c['domain']})")
    print(f"{c['website'] or 'no website'} | stage {c['stage'] or 'not given'} | "
          f"{c['employees'] if c['employees'] is not None else 'unknown number of'} employees")
    if c["locations"]:
        print("Locations: " + ", ".join(c["locations"][:6]))
    print("Sectors: " + (", ".join(c["sectors"]) or "none given"))
    print("Listed on: " + (", ".join(b["name"] or b["url"] for b in c["boards"]) or "no registered board any more"))
    print()
    if c["state"] == "tracked":
        print(f"Status: tracked, as {c['tracked_as']}" + (f" (activated {c['activated_at'][:10]})" if c["activated_at"] else
                                                          " (its website matches a company you already track)"))
    elif c["state"] == "dismissed":
        print(f"Status: dismissed on {c['dismissed_at'][:10]}" + (f": {c['dismiss_note']}" if c["dismiss_note"] else ""))
    else:
        print(f"Status: {'needs review' if c['state'] == 'review' else c['state']}: {c['reason']}")
    if c["checks"]:
        print("\nRules, in order:")
        labels = {"size": "Size", "stage": "Stage", "sector": "Sector", "roles": "Open roles"}
        for rule in cands.RULES:
            check = c["checks"].get(rule)
            if check:
                print(f"  {labels[rule] + ':':<12}{check['result']:<8} {check['detail']}")
    return 0


def _cmd_candidates_activate(args) -> int:
    conn = connect(args.db)
    if sum(bool(x) for x in (args.all, args.first, args.names)) != 1:
        print("error: say which candidates: names, --all, or --first N.", file=sys.stderr)
        return 1
    if args.names:
        chosen = [cands.get_candidate(conn, name) for name in args.names]
    else:
        chosen = cands.list_candidates(conn, "eligible")
        if args.first:
            chosen = chosen[:args.first]
    if not chosen:
        print("No eligible candidates to activate.")
        return 0
    done, failed = [], []
    for candidate in chosen:
        try:
            done.append(cands.activate(conn, candidate, force=args.force))
        except cands.CandidateError as exc:
            failed.append(str(exc))
    for d in done:
        print(f"Activated {d['name']} ({d['domain']}), tagged {', '.join(d['tags']) or 'with no board'}.")
    for message in failed:
        print(f"Not activated: {message}", file=sys.stderr)
    if done:
        print(f"\n{len(done)} activated. They have no job board mapped yet; find them with: "
              "rolescout companies resolve")
    return 1 if failed and not done else 0


def _cmd_candidates_dismiss(args) -> int:
    conn = connect(args.db)
    for name in args.names:
        candidate = cands.get_candidate(conn, name)
        cands.dismiss(conn, candidate, args.note or "")
        print(f"Dismissed {candidate['name']} ({candidate['domain']}); it will not be suggested again.")
    return 0


def _cmd_candidates_restore(args) -> int:
    conn = connect(args.db)
    for name in args.names:
        candidate = cands.get_candidate(conn, name)
        if cands.restore(conn, candidate):
            print(f"Restored {candidate['name']} ({candidate['domain']}).")
        else:
            print(f"{candidate['name']} was not dismissed.")
    return 0


def _cmd_daily(args) -> int:
    """What the scheduled job runs: scout (retrying if the network is not up yet), then backup."""
    if args.log:
        log = Path(args.log).expanduser()
        log.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(log)
        sys.stdout = sys.stderr = open(log, "a", buffering=1, encoding="utf-8")
    print(f"=== rolescout daily, {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')} ===")
    print(f"Database: {resolve_db_path(args.db)}")
    code = 0
    try:
        conn = connect(args.db)
        with jobs.track(conn, "daily") as job:
            code = _daily_steps(conn, args, job)
            if code is None:
                return 0
    except Exception as exc:  # the log must say why an unattended run died
        print(f"error: {type(exc).__name__}: {exc}")
        code = 1
    print(f"=== finished {datetime.now().astimezone().strftime('%H:%M:%S')}, "
          f"{'ok' if code == 0 else 'FAILED'} ===\n")
    return code


def _daily_steps(conn, args, job) -> int | None:
    """The daily job's work. Returns its exit code, or None when another scout
    was already running and this run was skipped."""
    code = 0
    job.advance(step="scout")
    for attempt in range(1, args.attempts + 1):
        try:
            summary = _scout_once(conn, args)
        except ScoutAlreadyRunning as exc:
            print(f"Not starting: {exc}. Skipping this run.")
            return None
        if not summary.boards or summary.ok:
            break
        # Every board failed: most often the Mac just woke and has no network yet.
        if attempt < args.attempts:
            print(f"Every board failed (attempt {attempt} of {args.attempts}); "
                  f"retrying in {args.retry_wait:g} seconds.")
            time.sleep(args.retry_wait)
        else:
            print(f"Every board failed on all {args.attempts} attempts.")
            code = 1
    # Backup before the re-check: the day's essential work is finished
    # and safe before the optional part starts.
    if args.backup_dir:
        job.advance(step="backup")
        _print_backup(conn, args.backup_dir, args.keep)
    if code == 0 and recheck_due(conn, args.recheck_days):
        job.advance(step="weekly coverage re-check")
        _weekly_recheck(conn, args)
    # Last, and optional: nothing it does can change the scout's result.
    if code == 0 and portfolio.harvest_due(conn, args.harvest_days):
        job.advance(step="weekly portfolio harvest")
        _weekly_harvest(conn, args)
    # The last word in the log: what this run changed.
    job.advance(step="digest")
    try:
        print("\n" + digest.digest_text(digest.build_digest(conn)))
    except Exception as exc:
        print(f"Digest not written: {type(exc).__name__}: {exc}")
    return code


# --- company profiles, sectors, VCs -----------------------------------------

def _sectors_path(args) -> Path:
    return profiles.mapping_path(resolve_db_path(args.db), getattr(args, "sectors_file", None))


def _mapping(conn, args) -> profiles.SectorMapping:
    return profiles.load_mapping(conn, _sectors_path(args))


def _print_profile(conn, profile: dict) -> None:
    def shown(field: str) -> str:
        value = profile[field]["value"]
        if field == "sectors":
            return ", ".join(value)
        if field == "employees" and value is not None:
            return f"{value:,}"
        return "" if value is None else str(value)
    rows = []
    for field in profiles.FIELDS:
        entry = profile[field]
        text = shown(field)
        if field == "stage" and entry["raw"] and entry["raw"] != entry["value"]:
            text += f"  (given as {entry['raw']})"
        rows.append([field, text or "-", entry["source"] or "", (entry["set_at"] or "")[:10]])
    print(profile["company"])
    print(format_table(["Field", "Value", "Source", "Set"], rows, {"Value": 90}))
    tags = profile["sectors"]["raw_tags"]
    if tags:
        print(f"\nBoard tags ({len(tags)}): {', '.join(tags)}")
    company_id = get_company(conn, profile["company"])["id"]
    links = vcs.company_links(conn, company_id)
    for kind, label in (("vc", "Backed by"), ("ecosystem", "Ecosystem groups")):
        names = [f"{l['name']} ({l['source']})" for l in links if l["type"] == kind]
        if names:
            print(f"{label}: {', '.join(names)}")


def _cmd_profile_show(args) -> int:
    conn = connect(args.db)
    profile = profiles.get_profile(conn, args.name)
    if args.json:
        print(json.dumps(profile, indent=2, ensure_ascii=False))
        return 0
    _print_profile(conn, profile)
    return 0


def _cmd_profile_set(args) -> int:
    conn = connect(args.db)
    profile = profiles.set_field(conn, args.name, args.field, " ".join(args.value), mapping=_mapping(conn, args))
    entry = profile[args.field]
    value = ", ".join(entry["value"]) if args.field == "sectors" else entry["value"]
    print(f"{profile['company']}: {args.field} set to {value}. Board and agent data will not replace it.")
    return 0


def _cmd_profile_clear(args) -> int:
    conn = connect(args.db)
    had = profiles.clear_field(conn, args.name, args.field)
    now = profiles.get_profile(conn, args.name)[args.field]
    name = get_company(conn, args.name)["name"]
    if not had:
        print(f"{name}: {args.field} had no value.")
    elif now["source"]:
        value = ", ".join(now["value"]) if args.field == "sectors" else now["value"]
        print(f"{name}: {args.field} cleared; the board's value applies again: {value}")
    else:
        print(f"{name}: {args.field} cleared.")
    return 0


def _coverage_lines(conn) -> list[str]:
    c = profiles.coverage(conn)
    total = c["companies"]
    lines = [f"Profiles of {total} active companies:"]
    for key, label in (("stage", "stage (not unknown)"), ("sectors", "at least one sector"),
                       ("description", "description"), ("employees", "employee count"), ("hq", "HQ location")):
        share = f"{100 * c[key] / total:.0f}%" if total else "-"
        lines.append(f"  {label + ':':<22}{c[key]:>6}  {share}")
    return lines


def _cmd_profile_missing(args) -> int:
    conn = connect(args.db)
    rows = profiles.missing(conn, args.field)
    if args.json:
        print(json.dumps(rows[:args.limit] if args.limit else rows, indent=2, ensure_ascii=False))
        return 0
    shown = rows[:args.limit] if args.limit else rows
    if not rows:
        print("Every active company has " + ("those fields" if args.field else "every profile field") + " filled.")
        return 0
    print(format_table(["Company", "Open roles", "Missing", "Website"],
                       [[r["company"], r["open_roles"], ", ".join(r["missing"]), r["website"] or ""] for r in shown]))
    print(f"\n{len(rows)} companies with a field missing" + (f", {len(shown)} shown" if len(shown) < len(rows) else "")
          + ". Set one with: rolescout profile set NAME FIELD VALUE")
    return 0


def _cmd_company(args) -> int:
    conn = connect(args.db)
    path = _ranking_path(args)
    try:
        cfg = ranking.load_config(path) if path.exists() else None
    except ConfigError:
        cfg = None
    data = overview.company_overview(conn, args.name, cfg, top=args.top)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    _print_profile(conn, profiles.get_profile(conn, data["company"]))
    print(f"\nWebsite: {data['website'] or '-'}   Job boards: {', '.join(data['job_boards']) or 'none mapped'}"
          + ("" if data["active"] else "   (deactivated)"))
    trend = data["hiring_30_days"]
    print(f"\nLast 30 days: {trend['opened']} opened, {trend['closed']} closed; {data['open_roles']} open now")
    if trend["weeks"]:
        print(format_table(["Week of", "Opened", "Closed", "Net", "Open at week end"],
                           [[w["week_start"], w["opened"], w["closed"], f"{w['net']:+d}", w["open_roles"]]
                            for w in trend["weeks"]]))
    roles = data["top_roles"]
    print(f"\nTop {len(roles)} of {data['open_roles']} open roles ({data['top_roles_ranked_by']}):")
    if roles:
        print(format_table(
            ["ID", "Score", "Title", "Location", "Posted", "Note", "URL"],
            [[r["role_id"], "" if r["score"] is None else f"{r['score']:g}", r["title"], r["location"],
              r["date_posted"] or "", " ".join(x for x in (r["mark"] or "", "" if r["qualifies"] in (True, None)
                                                            else "does not qualify") if x), r["url"]]
             for r in roles], {"Title": 52, "Location": 30}))
    else:
        print("  (none)")
    return 0


def _cmd_profile_coverage(args) -> int:
    conn = connect(args.db)
    if args.json:
        print(json.dumps(profiles.coverage(conn), indent=2))
        return 0
    print("\n".join(_coverage_lines(conn)))
    return 0


def _cmd_sectors_init(args) -> int:
    conn = connect(args.db)
    path = _sectors_path(args)
    if path.exists() and not args.force:
        print(f"error: {path} already exists; edit it, or pass --force to replace it", file=sys.stderr)
        return 1
    text = profiles.starter_mapping(conn)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    mapping = profiles.load_mapping(conn, path)
    counts = profiles.tag_counts(conn)
    loose = profiles.unmapped_tags(conn, mapping)
    print(f"Wrote {path}: {len(counts) - len(loose)} of the {len(counts)} tags in your data placed in "
          f"{len(mapping.sectors)} sectors, {len(loose)} left unmapped.")
    print("Edit it, then run: rolescout sectors apply")
    return 0


def _cmd_sectors_unmapped(args) -> int:
    conn = connect(args.db)
    mapping = _mapping(conn, args)
    loose = profiles.unmapped_tags(conn, mapping)
    if args.json:
        print(json.dumps([{"tag": t, "companies": n} for t, n in loose], indent=2, ensure_ascii=False))
        return 0
    source = mapping.source or "the built-in rules (no sectors.toml yet: rolescout sectors init)"
    if not loose:
        print(f"Every tag is mapped by {source}.")
        return 0
    shown = loose[:args.limit] if args.limit else loose
    print(format_table(["Tag", "Companies"], [[t, n] for t, n in shown]))
    print(f"\n{len(loose)} unmapped tags" + (f", {len(shown)} shown" if len(shown) < len(loose) else "")
          + f". Mapping: {source}")
    return 0


def _cmd_sectors_list(args) -> int:
    conn = connect(args.db)
    mapping = _mapping(conn, args)
    counts = {r["sector"]: r["n"] for r in conn.execute(
        "SELECT sector, COUNT(*) AS n FROM v_company_sectors GROUP BY sector")}
    print(format_table(["Sector", "Companies"], [[s, counts.get(s, 0)] for s in mapping.sectors]))
    print(f"\nMapping: {mapping.source or 'built-in rules (create a file with: rolescout sectors init)'}")
    return 0


def _cmd_sectors_apply(args) -> int:
    conn = connect(args.db)
    mapping = _mapping(conn, args)
    with transaction(conn):
        changed = profiles.backfill(conn, mapping)
    print(f"Sector mapping applied (no network): {changed} profile fields updated.")
    print("\n".join(_coverage_lines(conn)))
    return 0


def _cmd_vcs_list(args) -> int:
    rows = vcs.list_vcs(connect(args.db))
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("No VCs yet. Add one with: rolescout vcs add NAME, or register a board: rolescout portfolio add URL")
        return 0
    print(format_table(
        ["ID", "Name", "Short name", "Type", "Website", "Companies", "By hand", "Boards"],
        [[r["id"], r["name"], r["short_name"], r["type"], r["website"], r["companies"], r["linked_by_hand"],
          ", ".join(r["boards"])] for r in rows], {"Boards": 70}))
    return 0


def _cmd_vcs_add(args) -> int:
    vc = vcs.add_vc(connect(args.db), args.name, short=args.short or "", kind=args.type, website=args.website or "")
    print(f"Added {vc['name']} (shown as {vc['short_name']}, type {vc['type']}).")
    return 0


def _cmd_vcs_edit(args) -> int:
    if args.name is None and args.short is None and args.type is None and args.website is None:
        print("error: say what to change: --name, --short, --type or --website.", file=sys.stderr)
        return 1
    vc = vcs.edit_vc(connect(args.db), args.vc, name=args.name, short=args.short, kind=args.type,
                     website=args.website)
    print(f"{vc['name']}: shown as {vc['short_name']}, type {vc['type']}, website {vc['website'] or 'none'}.")
    return 0


def _cmd_vcs_remove(args) -> int:
    name, links, boards = vcs.remove_vc(connect(args.db), args.vc)
    print(f"Removed {name}: {links} company link{'' if links == 1 else 's'} removed, "
          f"{boards} board{'' if boards == 1 else 's'} left registered with no VC.")
    return 0


def _cmd_vcs_attach(args) -> int:
    url, name = vcs.attach_board(connect(args.db), args.board, args.vc)
    print(f"{url} now belongs to {name}.")
    return 0


def _cmd_vcs_link(args) -> int:
    company, name, new = vcs.link_company(connect(args.db), args.company, args.vc)
    print(f"{company} linked to {name} by hand." if new else f"{company} was already linked to {name} by hand.")
    return 0


def _cmd_vcs_unlink(args) -> int:
    conn = connect(args.db)
    company, name, removed = vcs.unlink_company(conn, args.company, args.vc)
    if not removed:
        print(f"{company} was not linked to {name}.")
    else:
        print(f"{company} is no longer linked to {name}. A board that lists it will not bring the link back; "
              f"to restore it: rolescout vcs link {json.dumps(company)} {json.dumps(name)}")
    return 0


def _cmd_digest(args) -> int:
    data = digest.build_digest(connect(args.db), since=args.since)
    print(json.dumps(data, indent=2, ensure_ascii=False) if args.json else digest.digest_text(data))
    return 0


def _cmd_status(args) -> int:
    conn = connect(args.db)
    status = collect_status(conn, schedule=launchd.read_schedule())
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0 if status["healthy"] else 1

    print("HEALTHY" if status["healthy"] else "NEEDS ATTENTION")
    for item in status["attention"]:
        print(f"  ! {item}")
    lines = []
    last = status["last_run"]
    if last:
        lines.append(("Last run", f"{last['started_at']} ({last['hours_ago']:g} hours ago), {last['status']}"))
        lines.append(("Companies", f"{last['companies_ok']} ok, {last['companies_failed']} failed "
                                   f"({last['boards_ok']} boards ok, {last['boards_failed']} failed)"))
        lines.append(("Changes", f"{last['roles_opened']} opened, {last['roles_closed']} closed in that run"))
    else:
        lines.append(("Last run", "never"))
    lines.append(("Right now", status["now"]["line"]))
    failing = status["failing_boards"]
    lines.append(("Failing boards", f"{len(failing)} failing more than two runs in a row"
                  if failing else "none failing more than two runs in a row"))
    partial = status["partial_reads"]
    lines.append(("Partial reads", f"{len(partial)} board{'' if len(partial) == 1 else 's'} last read below 99% "
                                   "of the roles listed; nothing closes there until a fuller read"
                  if partial else "none (every board was last read to 99% or more of what it lists)"))
    lines.append(("Open roles", f"{status['open_roles']:,} across {status['companies_mapped']} mapped "
                                f"companies ({status['companies_active']} active)"))
    lines.append(("Database", f"{status['database']} ({_human_bytes(status['database_bytes'])})"))
    lines.append(("Coverage gaps", status["gaps"]["summary"] + "  (rolescout gaps)"))
    harvest = status["last_harvest"]
    if harvest:
        lines.append(("Last harvest", harvest["at"] + (
            f"; {harvest['activated']} auto-activated, {harvest['mapped']} of them mapped to a job board"
            if harvest["auto_activate"] else "; auto-activation is off")))
    backup = status["last_backup"]
    if backup:
        lines.append(("Last backup", f"{backup['path']} ({_human_bytes(backup['bytes'])}, "
                                     f"{backup['modified']}, {backup['count']} kept)"))
    sched = status["schedule"]
    if not sched["supported"]:
        lines.append(("Next run", "not scheduled (rolescout schedules itself on macOS only)"))
    elif sched.get("loaded"):
        lines.append(("Next run", f"{sched['next_run']} local time (daily at {sched['time']})"))
        lines.append(("Log", sched["log"]))
        if sched.get("database") and Path(sched["database"]) != resolve_db_path(args.db):
            lines.append(("Note", f"the schedule runs against a different database: {sched['database']}"))
    elif sched.get("installed"):
        lines.append(("Next run", sched.get("error") or "schedule file present but not loaded"))
    else:
        lines.append(("Next run", "not scheduled (rolescout schedule install --at HH:MM)"))
    for label, value in lines:
        print(f"{label + ':':<16}{value}")
    if partial:
        print("\nRead below 99% of the roles listed:")
        print(format_table(["Company", "Board", "Seen", "Listed", "Share", "Scout"],
                           [[p["company"], f"{p['source']}/{p['ref']}", p["roles_seen"], p["roles_reported"],
                             f"{p['percent']}%", p["started_at"][:16].replace("T", " ")] for p in partial]))
    if last and last["failed"]:
        print("\nFailed in the last run:")
        print(format_table(["Company", "Board", "Error"],
                           [[f["name"], f"{f['source']}/{f['ref']}", f["error"]] for f in last["failed"]],
                           {"Error": 80}))
    return 0 if status["healthy"] else 1


def _cmd_schedule_install(args) -> int:
    hour, minute = launchd.parse_time(args.at)
    db = resolve_db_path(args.db).resolve()
    info = launchd.install(
        hour, minute, db=db, keep=args.keep,
        backup_dir=Path(args.backup_dir).expanduser().resolve(),
        log_dir=Path(args.log_dir).expanduser().resolve())
    connect(db).close()  # make sure the database exists before the first run
    print(f"Scheduled: rolescout scouts daily at {hour:02d}:{minute:02d} local time, then backs up.")
    print(f"  Next run:  {launchd.next_run(hour, minute).strftime('%Y-%m-%d %H:%M')}")
    print(f"  Database:  {db}")
    print(f"  Log:       {info['log']}")
    print(f"  Backups:   {info['backup_dir']} (last {args.keep} kept)")
    print(f"  Schedule:  {info['plist']} (launchd domain {info['domain']})")
    if info["domain"].startswith("user/"):
        print("  Note: loaded without a desktop session. If runs do not happen, log in at the "
              "Mac's screen once (or enable automatic login) and install again.")
    return 0


def _cmd_schedule_remove(args) -> int:
    if launchd.remove():
        print(f"Schedule removed ({launchd.plist_path()} deleted). Logs, backups and the database are kept.")
    else:
        print("No schedule was installed.")
    return 0


# --- query ------------------------------------------------------------------

def _cmd_query(args) -> int:
    conn = connect(args.db)
    if args.company:
        get_company(conn, args.company)  # a typo should fail loudly, not match nothing
    try:
        roles = query_roles(conn, company=args.company, title=args.title, status=args.status,
                            since=args.since, include_inactive=args.include_inactive,
                            limit=args.limit, only=_filter(conn, args))
    except ValueError:
        print(f"error: --since must be a date like 2026-01-31, got {args.since!r}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(roles, indent=2, ensure_ascii=False))
        return 0
    headers = ["ID", "Company", "Title", "Location", "Posted", "First seen", "Status", "URL"]
    rows = [[r["id"], r["company"], r["title"], r["location"], r["date_posted"], r["first_seen_at"][:10],
             r["status"] if r["status"] == "open" else f"closed {r['closed_at'][:10]}", r["url"]]
            for r in roles]
    print(format_table(headers, rows, {"Company": 20, "Title": 55, "Location": 30}))
    print(f"\n{len(roles)} role{'' if len(roles) == 1 else 's'}.")
    return 0


# --- parser -----------------------------------------------------------------

def _add_filter_flags(parser: argparse.ArgumentParser, title: str) -> None:
    group = parser.add_argument_group("filters", title)
    group.add_argument("--stage", action="append", metavar="STAGE",
                       help="company stage; repeat or separate with commas for any of several: "
                            + ", ".join(profiles.STAGES))
    group.add_argument("--sector", action="append", metavar="SECTOR",
                       help="company sector (rolescout sectors list); repeat for any of several")
    group.add_argument("--hq", metavar="PLACE", help="text the company's HQ location contains")
    group.add_argument("--location", metavar="PLACE",
                       help='where the role is: a place one of its locations names ("United States" '
                            'covers every US form), or "remote"')
    group.add_argument("--employees", metavar="RANGE", help="company size: 50-500, 200- or -50")
    group.add_argument("--vc", metavar="VC", help="companies linked to this VC or ecosystem group")


# Help for the arguments that mean the same thing wherever they appear.
_COMMON_HELP = {
    "json": "print the result as JSON",
    "name": "the company's name, as `rolescout companies list` shows it",
    "company": "the company's name, as `rolescout companies list` shows it",
    "source": "the job system",
    "ref": "the board's reference, as `rolescout companies list` shows it",
    "website": "its website, e.g. acme.example",
    "id": "the role's ID, from the ID column",
    "status": "the mark: dismissed, saved or applied",
    "limit": "show at most this many",
    "names": "candidates, by name",
    "field": "the profile field: description, sectors, stage, employees or hq",
    "vc": "the VC, by name or short name",
    "delay": "minimum pause between requests, in seconds (default: 1.0)",
    "keep": "backups to keep (default: 7)",
    "retry_wait": "seconds between tries when every board fails (default: 120)",
    "short": "the short name shown in tables",
    "type": "vc or ecosystem",
}


def _fill_help(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
    """Give every argument that has no help text of its own the common one."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                _fill_help(sub, (*path, name))
        elif action.help is None:
            text = _COMMON_HELP.get(action.dest)
            if path[:1] == ("vcs",) and action.dest == "name":
                text = "the VC's name" if path[1:] == ("add",) else "a new name"
            if path[:2] == ("vcs", "edit") and action.dest == "website":
                text = "a new website"
            action.help = text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rolescout", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Fetch open roles from the job boards of the companies you track into a local "
                    "database, and rank a shortlist of the ones to look at first.",
        epilog="First steps:\n"
               "  rolescout init         starter companies, their boards and starter configs\n"
               "  rolescout scout        fetch every mapped board\n"
               "  rolescout shortlist    the roles to look at first\n"
               "  rolescout status       health, last run and what is running now\n"
               "Every command takes --help. Guides: README.md, DEPLOY.md and MCP.md in the repository.")
    parser.add_argument("--version", action="version", version=f"rolescout {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    db = argparse.ArgumentParser(add_help=False)
    db.add_argument("--db", metavar="PATH",
                    help=f"database file (default: ${DB_ENV_VAR}, else {DEFAULT_DB_PATH})")

    init = commands.add_parser(
        "init", parents=[db],
        help="set up a new install: starter companies with their boards, and starter ranking "
             "and candidates configs")
    init.add_argument("--no-companies", action="store_true",
                      help="do not add the starter companies (configs only)")
    init.add_argument("--portfolio", action="store_true",
                      help="also register a few example portfolio boards (see: rolescout portfolio)")
    init.add_argument("--force", action="store_true",
                      help="replace existing ranking and candidates configs with the starters")
    init.set_defaults(func=_cmd_init)

    fetch = commands.add_parser(
        "fetch", help="fetch one board live and print the role count and a sample role")
    fetch.add_argument("source", choices=sorted(ADAPTERS))
    fetch.add_argument("slug", help="board slug, e.g. the last part of jobs.lever.co/<slug>; "
                                    "for workday, TENANT/wdN/SITE or the career site's URL; "
                                    "for rippling, gem and workable, any URL of the board also works; "
                                    "for eightfold, SITE-HOST/COMPANY-DOMAIN or a URL of the site; "
                                    "for google, careers")
    fetch.add_argument("--sample", type=int, default=1, metavar="N",
                       help="which role to print, 1-based (default: 1)")
    fetch.add_argument("--full", action="store_true", help="print the whole description")
    fetch.set_defaults(func=_cmd_fetch)

    companies = commands.add_parser("companies", help="manage target companies and board mappings")
    sub = companies.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")

    p = sub.add_parser(
        "import", parents=[db],
        help="import companies: a CSV (company_name, website) or a .json export with boards")
    p.add_argument("file", help="CSV file, .json export, or - to read an export from standard input")
    p.set_defaults(func=_cmd_companies_import)

    p = sub.add_parser(
        "export", parents=[db],
        help="write companies and their board mappings to a JSON file (personal data)")
    p.add_argument("file", help="where to write it, or - for standard output")
    p.set_defaults(func=_cmd_companies_export)

    p = sub.add_parser("list", parents=[db], help="list companies with their board and status")
    p.add_argument("--active", action="store_true", help="only active companies")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_companies_list)

    p = sub.add_parser("add", parents=[db], help="add one company")
    p.add_argument("name")
    p.add_argument("--website", default="")
    p.add_argument("--tier", type=int, help=argparse.SUPPRESS)      # accepted for old scripts; ignored
    p.add_argument("--tags", help="comma-separated, e.g. ai,fintech")
    p.set_defaults(func=_cmd_companies_add)

    p = sub.add_parser("set-website", parents=[db],
                       help="correct a company's website, e.g. set-website Acme acme.example")
    p.add_argument("name")
    p.add_argument("url", help='the website; "example.com" becomes https://example.com')
    p.set_defaults(func=_cmd_companies_set_website)

    p = sub.add_parser("deactivate", parents=[db], help="stop scouting a company (keeps its history)")
    p.add_argument("name")
    p.set_defaults(func=_cmd_companies_set_active, active=False)

    p = sub.add_parser("activate", parents=[db], help="resume scouting a deactivated company")
    p.add_argument("name")
    p.set_defaults(func=_cmd_companies_set_active, active=True)

    p = sub.add_parser("map", parents=[db],
                       help="add a board to a company, e.g. map Acme greenhouse acme")
    p.add_argument("name")
    p.add_argument("source", choices=sorted(ADAPTERS))
    p.add_argument("ref", help="board reference: the slug; for workday, TENANT/wdN/SITE "
                               "or any URL on the career site; for rippling, gem and workable, "
                               "any URL of the board or of one of its jobs also works; for eightfold, "
                               "SITE-HOST/COMPANY-DOMAIN (e.g. acme.eightfold.ai/acme.com) or a URL "
                               "of the career site; for google, careers")
    p.set_defaults(func=_cmd_companies_map)

    p = sub.add_parser("unmap", parents=[db],
                       help="remove a board from a company and close its open roles")
    p.add_argument("name")
    p.add_argument("source", choices=sorted(ADAPTERS))
    p.add_argument("ref")
    p.set_defaults(func=_cmd_companies_unmap)

    p = sub.add_parser(
        "resolve", parents=[db],
        help="find boards for unmapped companies; saves only high-confidence matches")
    p.add_argument("names", nargs="*", metavar="NAME",
                   help="companies to resolve (default: every active company with no board)")
    p.add_argument("--dry-run", action="store_true", help="show what would be saved; write nothing")
    p.add_argument("--max-minutes", type=float, metavar="MINUTES",
                   help="stop starting new companies after this long (default: no limit)")
    p.add_argument("--delay", type=float, default=1.0, metavar="SECONDS",
                   help="minimum pause between requests (default: 1.0)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_companies_resolve)

    p = sub.add_parser(
        "recheck", parents=[db],
        help="resolve unmapped companies and companies whose board keeps failing "
             "(the daily job does this weekly)")
    p.add_argument("--dry-run", action="store_true", help="show what would be saved; write nothing")
    p.add_argument("--max-minutes", type=float, metavar="MINUTES",
                   help="stop starting new companies after this long (default: no limit)")
    p.add_argument("--delay", type=float, default=1.0, metavar="SECONDS")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_companies_resolve, recheck=True, names=[])

    p = sub.add_parser(
        "set-system", parents=[db],
        help="record by hand which job system a company uses; detection never overwrites it")
    p.add_argument("name")
    p.add_argument("system", help="e.g. icims, rippling, paycom, or custom for a home-built site")
    p.add_argument("url", help="where its jobs are listed")
    p.add_argument("--note", help="anything worth keeping, e.g. how you found it")
    p.set_defaults(func=_cmd_companies_set_system)

    p = sub.add_parser("clear-system", parents=[db],
                       help="forget a hand-recorded job system so detection applies again")
    p.add_argument("name")
    p.set_defaults(func=_cmd_companies_clear_system)

    p = sub.add_parser(
        "report", parents=[db],
        help="mapping status of all companies: mapped, unsupported job system, unresolved")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_companies_report)

    scout = commands.add_parser(
        "scout", parents=[db], help="fetch every active, mapped company and record the run")
    scout.add_argument("--delay", type=float, default=1.0, metavar="SECONDS",
                       help="minimum pause between requests (default: 1.0)")
    scout.add_argument("--json", action="store_true", help="print the run summary as JSON")
    scout.set_defaults(func=_cmd_scout)

    config = argparse.ArgumentParser(add_help=False)
    config.add_argument(
        "--config", metavar="PATH",
        help=f"ranking config (default: ${ranking.CONFIG_ENV_VAR}, else {ranking.CONFIG_NAME} next to the database)")

    sl = commands.add_parser(
        "shortlist", parents=[db, config],
        help="the top open roles to look at first (rebuilt after every scout)")
    sl.add_argument("--top", type=int, metavar="N", help="show only the first N")
    sl.add_argument("--by-company", action="store_true",
                    help="group by company, companies with the best-ranked roles first")
    sl.add_argument("--new", action="store_true", help="only roles that were not on the previous list")
    sl.add_argument("--since", metavar="YYYY-MM-DD",
                    help="compare with the list of this day instead of the previous one (30 days are kept)")
    sl.add_argument("--json", action="store_true")
    _add_filter_flags(sl, "With any of these, every qualifying role that passes is ranked now, "
                          "not only today's list:")
    sl.set_defaults(func=_cmd_shortlist)
    slsub = sl.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    # The same two flags, accepted after the subcommand as well. No default
    # here, so a value given before the subcommand is not wiped out.
    late = argparse.ArgumentParser(add_help=False)
    late.add_argument("--db", metavar="PATH", default=argparse.SUPPRESS, help="database file")
    late.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help="ranking config")
    p = slsub.add_parser("init", parents=[late], help="create a starter ranking config")
    p.add_argument("--filters", metavar="CSV",
                   help="seed titles and seniority from a role_filters.csv-style file "
                        "(field,value rows) instead of the starter set")
    p.add_argument("--force", action="store_true", help="replace an existing config")
    p.set_defaults(func=_cmd_shortlist_init)
    p = slsub.add_parser("rebuild", parents=[late],
                         help="re-rank now from the database and the config (no network)")
    p.set_defaults(func=_cmd_shortlist_rebuild)

    p = commands.add_parser(
        "mark", parents=[db, config],
        help="mark a role dismissed, saved or applied; dismissed and applied cover the same job's postings "
             "for other locations")
    p.add_argument("id", help="the role's ID, from the ID column")
    p.add_argument("status", choices=ranking.MARKS)
    p.add_argument("--note", help="anything you want to remember about it")
    p.set_defaults(func=_cmd_mark)

    p = commands.add_parser("unmark", parents=[db, config], help="remove a role's mark")
    p.add_argument("id")
    p.set_defaults(func=_cmd_unmark)

    p = commands.add_parser("marks", parents=[db], help="list the roles you have marked")
    p.add_argument("--status", choices=ranking.MARKS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_marks)

    p = commands.add_parser(
        "why", parents=[db, config],
        help="explain a role's score and its place on the shortlist")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_why)

    p = commands.add_parser(
        "serve", parents=[db, config],
        help="run the MCP server over the database (needs: pip install \"rolescout[mcp]\")")
    p.add_argument("--http", action="store_true",
                   help="serve over HTTP for clients on other machines (default: stdio, for a "
                        "client on this machine that starts the server itself)")
    p.add_argument("--host", metavar="ADDRESS",
                   help="with --http: address to listen on (default: 127.0.0.1, this machine only). "
                        "For Tailscale pass this machine's tailnet address")
    p.add_argument("--port", type=int, metavar="PORT", help="with --http: port (default: 8765)")
    p.add_argument("--allow-host", action="append", metavar="NAME",
                   help="with --http: an extra hostname clients may use, e.g. a Tailscale name "
                        "(mac.tailnet.ts.net). Repeatable")
    p.add_argument("--token-file", metavar="PATH",
                   help="with --http: read the access token from this file instead of "
                        "$ROLESCOUT_MCP_TOKEN")
    p.add_argument("--read-only", action="store_true",
                   help="offer only the tools that read; every tool that changes anything is absent")
    p.set_defaults(func=_cmd_serve)

    filt = argparse.ArgumentParser(add_help=False)
    filt.add_argument(
        "--config", metavar="PATH",
        help=f"candidates filter (default: ${cands.CONFIG_ENV_VAR}, else {cands.CONFIG_NAME} next to the database)")

    pf = commands.add_parser(
        "portfolio", help="portfolio job boards you read for new companies (never for roles)")
    pfsub = pf.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")
    p = pfsub.add_parser("add", parents=[db], help="register a board by its URL")
    p.add_argument("url", help="the board's address, e.g. https://jobs.example.vc")
    p.add_argument("--name", help="what to call it (default: the name the board gives itself)")
    p.add_argument("--vc", metavar="VC", help="the VC it belongs to (default: a VC is made from the board)")
    p.set_defaults(func=_cmd_portfolio_add)
    p = pfsub.add_parser("list", parents=[db], help="list registered boards")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_portfolio_list)
    p = pfsub.add_parser("remove", parents=[db], help="unregister a board; its candidates are kept")
    p.add_argument("board", help="the board's ID, URL or name")
    p.set_defaults(func=_cmd_portfolio_remove)
    p = pfsub.add_parser(
        "harvest", parents=[db, filt],
        help="read the boards' companies into the candidates list and apply the filter")
    p.add_argument("boards", nargs="*", metavar="BOARD", help="only these boards (default: all)")
    p.add_argument("--max-minutes", type=float, default=30.0, metavar="MINUTES",
                   help="stop after this long; the next harvest continues (default: 30, 0 for no limit)")
    p.add_argument("--delay", type=float, default=1.0, metavar="SECONDS",
                   help="minimum pause between requests (default: 1.0)")
    p.set_defaults(func=_cmd_portfolio_harvest)

    cd = commands.add_parser(
        "candidates", parents=[db, filt],
        help="companies found on portfolio boards: eligible, needing review, or rejected")
    cd.add_argument("--status", choices=cands.STATUSES, help="only candidates with this status")
    cd.add_argument("--limit", type=int, metavar="N")
    cd.add_argument("--json", action="store_true")
    cd.set_defaults(func=_cmd_candidates)
    cdsub = cd.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    late_c = argparse.ArgumentParser(add_help=False)
    late_c.add_argument("--db", metavar="PATH", default=argparse.SUPPRESS, help="database file")
    late_c.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help="candidates filter")
    p = cdsub.add_parser("init", parents=[late_c], help="create a starter filter config")
    p.add_argument("--filters", metavar="CSV",
                   help="seed sector keywords from the domain rows of a role_filters.csv-style file "
                        "instead of the starter set")
    p.add_argument("--force", action="store_true", help="replace an existing config")
    p.set_defaults(func=_cmd_candidates_init)
    p = cdsub.add_parser("apply", parents=[late_c],
                         help="re-apply the filter to every candidate (no network)")
    p.set_defaults(func=_cmd_candidates_apply)
    p = cdsub.add_parser("why", parents=[late_c], help="show each rule's result for one candidate")
    p.add_argument("name", help="the candidate's name or website domain")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(func=_cmd_candidates_why)
    p = cdsub.add_parser(
        "activate", parents=[late_c],
        help="start tracking candidates: adds them as companies tagged with their boards")
    p.add_argument("names", nargs="*", metavar="NAME", help="candidates by name or domain")
    p.add_argument("--all", action="store_true", help="every eligible candidate")
    p.add_argument("--first", type=int, metavar="N", help="the first N eligible candidates, as listed")
    p.add_argument("--force", action="store_true", help="allow a rejected or dismissed candidate named here")
    p.set_defaults(func=_cmd_candidates_activate)
    p = cdsub.add_parser("dismiss", parents=[late_c], help="never suggest these candidates again")
    p.add_argument("names", nargs="+", metavar="NAME")
    p.add_argument("--note", help="why, for your own reference")
    p.set_defaults(func=_cmd_candidates_dismiss)
    p = cdsub.add_parser("restore", parents=[late_c], help="undo a dismissal")
    p.add_argument("names", nargs="+", metavar="NAME")
    p.set_defaults(func=_cmd_candidates_restore)

    p = commands.add_parser(
        "gaps", parents=[db],
        help="companies we cannot fetch, grouped by job system, with connectors worth building")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_gaps)

    p = commands.add_parser(
        "brief", parents=[db],
        help="print a connector brief for one unsupported job system (Markdown, stays local)")
    p.add_argument("system", help="a job system named in: rolescout gaps")
    p.set_defaults(func=_cmd_brief)

    p = commands.add_parser(
        "status", parents=[db],
        help="health at a glance, no network: what is running now, last run, failures, open roles, next run")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_status)

    p = commands.add_parser(
        "company", parents=[db, config],
        help="one company at a glance: profile, VCs, its top open roles and 30 days of hiring")
    p.add_argument("name")
    p.add_argument("--top", type=int, default=5, metavar="N", help="how many roles to show (default: 5)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_company)

    pr = commands.add_parser("profile", help="company profiles: description, sectors, stage, employees, HQ")
    prsub = pr.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")
    mapping_flag = argparse.ArgumentParser(add_help=False)
    mapping_flag.add_argument("--sectors-file", metavar="PATH", default=argparse.SUPPRESS,
                              help="sector mapping file (default: sectors.toml next to the database)")
    p = prsub.add_parser("show", parents=[db], help="a company's profile, with where each value came from")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_profile_show)
    p = prsub.add_parser("set", parents=[db, mapping_flag],
                         help="set a field by hand; board and agent data never replace it")
    p.add_argument("name")
    p.add_argument("field", choices=profiles.FIELDS)
    p.add_argument("value", nargs="+", help="the value; for sectors, a comma-separated list; for stage, one of: "
                                            + ", ".join(profiles.STAGES))
    p.set_defaults(func=_cmd_profile_set)
    p = prsub.add_parser("clear", parents=[db], help="forget a field's value, so board data applies again")
    p.add_argument("name")
    p.add_argument("field", choices=profiles.FIELDS)
    p.set_defaults(func=_cmd_profile_clear)
    p = prsub.add_parser("missing", parents=[db], help="companies with empty profile fields, most open roles first")
    p.add_argument("--field", action="append", choices=profiles.FIELDS,
                   help="only look at this field; repeat for several (default: all)")
    p.add_argument("--limit", type=int, default=40, metavar="N", help="rows to show (default: 40; 0 for all)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_profile_missing)
    p = prsub.add_parser("coverage", parents=[db], help="how many companies have each field filled")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_profile_coverage)

    sc = commands.add_parser("sectors", help="the sector list and the mapping from raw board tags to it")
    scsub = sc.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")
    p = scsub.add_parser("init", parents=[db, mapping_flag],
                         help="write a starter sectors.toml from the tags in your data")
    p.add_argument("--force", action="store_true", help="replace an existing file")
    p.set_defaults(func=_cmd_sectors_init)
    p = scsub.add_parser("list", parents=[db, mapping_flag], help="the sectors, with how many companies are in each")
    p.set_defaults(func=_cmd_sectors_list)
    p = scsub.add_parser("unmapped", parents=[db, mapping_flag],
                         help="raw tags no sector claims, most common first")
    p.add_argument("--limit", type=int, default=40, metavar="N", help="rows to show (default: 40; 0 for all)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_sectors_unmapped)
    p = scsub.add_parser("apply", parents=[db, mapping_flag],
                         help="map every company's tags again after editing the file (no network)")
    p.set_defaults(func=_cmd_sectors_apply)

    vc = commands.add_parser("vcs", help="the VC list: investors, ecosystem groups, their boards and links")
    vcsub = vc.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")
    p = vcsub.add_parser("list", parents=[db], help="every VC with its boards and linked companies")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_vcs_list)
    p = vcsub.add_parser("add", parents=[db], help="add a VC or an ecosystem group")
    p.add_argument("name")
    p.add_argument("--short", help="the name Backed by shows (default: the name without Ventures, Capital, ...)")
    p.add_argument("--type", choices=vcs.TYPES, default="vc",
                   help="ecosystem: a group that lists companies it did not back; left out of Backed by")
    p.add_argument("--website")
    p.set_defaults(func=_cmd_vcs_add)
    p = vcsub.add_parser("edit", parents=[db], help="change a VC's name, short name, type or website")
    p.add_argument("vc", help="the VC's name, short name or ID")
    p.add_argument("--name")
    p.add_argument("--short")
    p.add_argument("--type", choices=vcs.TYPES)
    p.add_argument("--website")
    p.set_defaults(func=_cmd_vcs_edit)
    p = vcsub.add_parser("remove", parents=[db], help="remove a VC and its links; its boards stay registered")
    p.add_argument("vc")
    p.set_defaults(func=_cmd_vcs_remove)
    p = vcsub.add_parser("attach", parents=[db], help="say which VC a portfolio board belongs to")
    p.add_argument("board", help="the board's ID, URL or name (rolescout portfolio list)")
    p.add_argument("vc")
    p.set_defaults(func=_cmd_vcs_attach)
    p = vcsub.add_parser("link", parents=[db], help="link a company to a VC by hand")
    p.add_argument("company")
    p.add_argument("vc")
    p.set_defaults(func=_cmd_vcs_link)
    p = vcsub.add_parser("unlink", parents=[db], help="remove a link, however it was made; a board does not bring it back")
    p.add_argument("company")
    p.add_argument("vc")
    p.set_defaults(func=_cmd_vcs_unlink)

    p = commands.add_parser(
        "digest", parents=[db],
        help="what changed since the previous completed scout: roles, shortlist, companies, coverage")
    p.add_argument("--since", metavar="DATE",
                   help="compare with the last scout completed before this day began (YYYY-MM-DD)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_digest)

    p = commands.add_parser(
        "backup", parents=[db],
        help="write a consistent copy of the database (safe during a scout)")
    p.add_argument("directory", help="folder to write rolescout-YYYYMMDD-HHMMSS.db into")
    p.add_argument("--keep", type=int, default=7, metavar="N",
                   help="how many backups to keep in that folder (default: 7)")
    p.set_defaults(func=_cmd_backup)

    sched = commands.add_parser("schedule", help="daily scheduled scout (macOS launchd)")
    ssub = sched.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")
    p = ssub.add_parser("install", parents=[db],
                        help="run scout and backup every day at a set time; replaces any existing schedule")
    p.add_argument("--at", required=True, metavar="HH:MM", help="24-hour local time, e.g. 07:30")
    p.add_argument("--backup-dir", default=str(launchd.DEFAULT_BACKUP_DIR), metavar="DIR",
                   help=f"where the daily backup goes (default: {launchd.DEFAULT_BACKUP_DIR})")
    p.add_argument("--keep", type=int, default=7, metavar="N", help="backups to keep (default: 7)")
    p.add_argument("--log-dir", default=str(launchd.DEFAULT_LOG_DIR), metavar="DIR",
                   help=f"where the log goes (default: {launchd.DEFAULT_LOG_DIR})")
    p.set_defaults(func=_cmd_schedule_install)
    p = ssub.add_parser("remove", help="remove the daily schedule; keeps logs, backups and data")
    p.set_defaults(func=_cmd_schedule_remove)

    p = commands.add_parser(
        "daily", parents=[db],
        help="what the schedule runs: scout, retry if offline, then backup")
    p.add_argument("--backup-dir", metavar="DIR", help="back up here after the scout")
    p.add_argument("--keep", type=int, default=7, metavar="N")
    p.add_argument("--log", metavar="FILE", help="append output to this file (rotated at 1 MB, 3 kept)")
    p.add_argument("--delay", type=float, default=1.0, metavar="SECONDS")
    p.add_argument("--attempts", type=int, default=3, metavar="N",
                   help="tries when every board fails, e.g. no network after wake (default: 3)")
    p.add_argument("--retry-wait", type=float, default=120.0, metavar="SECONDS")
    p.add_argument("--recheck-days", type=float, default=7.0, metavar="DAYS",
                   help="re-check coverage (resolve unmapped companies and failing boards) "
                        "this often; 0 turns it off (default: 7)")
    p.add_argument("--recheck-minutes", type=float, default=15.0, metavar="MINUTES",
                   help="upper limit on one coverage re-check (default: 15)")
    p.add_argument("--harvest-days", type=float, default=7.0, metavar="DAYS",
                   help="read the registered portfolio boards this often; 0 turns it off (default: 7)")
    p.add_argument("--harvest-minutes", type=float, default=15.0, metavar="MINUTES",
                   help="upper limit on one day's portfolio harvest (default: 15)")
    p.set_defaults(func=_cmd_daily)

    query = commands.add_parser(
        "query", parents=[db], help="search stored roles (no network)")
    query.add_argument("--company", help="exact company name (case-insensitive)")
    query.add_argument("--title", action="append", metavar="PHRASE",
                       help="title contains this phrase; repeat to match any of several")
    status = query.add_mutually_exclusive_group()
    status.add_argument("--open", dest="status", action="store_const", const="open",
                        help="open roles only (default)")
    status.add_argument("--closed", dest="status", action="store_const", const="closed",
                        help="closed roles only")
    status.add_argument("--all", dest="status", action="store_const", const="all",
                        help="open and closed roles")
    query.add_argument("--since", metavar="YYYY-MM-DD",
                       help="only roles first seen on or after this date")
    query.add_argument("--include-inactive", action="store_true",
                       help="also show open roles of deactivated companies (not refreshed, may be stale)")
    query.add_argument("--limit", type=int, metavar="N")
    _add_filter_flags(query, "Narrow by the company's profile, its VC, or the role's place:")
    query.add_argument("--json", action="store_true")
    query.set_defaults(func=_cmd_query, status="open")
    _fill_help(parser, ())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CompanyError, SchemaError, FileNotFoundError, TransferError, BackupError,
            launchd.ScheduleError, GapsError, ConfigError, MarkError, cands.ConfigError,
            cands.CandidateError, portfolio.PortfolioError, digest.DigestError,
            profiles.ProfileError, profiles.MappingError, vcs.VcError, filters.FilterError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # The reader went away (| head): stop quietly. Point stdout at nothing
        # so the interpreter's own flush at exit has no closed pipe to fail on.
        _silence_stdout()
        return 0
    finally:
        try:
            sys.stdout.flush()
        except BrokenPipeError:
            _silence_stdout()


def _silence_stdout() -> None:
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass   # stdout is not a real file (a test's capture): nothing to protect


if __name__ == "__main__":
    sys.exit(main())
