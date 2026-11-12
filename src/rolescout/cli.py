"""The ``rolescout`` console command."""

import argparse
import json
import sys

from rolescout import __version__
from rolescout.adapters import ADAPTERS, SourceError, get_adapter
from rolescout.companies import (
    CompanyError, add_board, add_company, get_company, import_csv, list_companies, remove_board,
    set_active,
)
from rolescout.db import DB_ENV_VAR, DEFAULT_DB_PATH, SchemaError, connect, resolve_db_path
from rolescout.query import query_roles
from rolescout.resolve import Resolution, mapping_report, resolve_companies
from rolescout.scout import BoardResult, run_scout

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
    try:
        roles = get_adapter(args.source).fetch(args.slug)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"{args.source}/{args.slug}: {len(roles)} open roles")
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
    added, updated = import_csv(conn, args.csv)
    print(f"Imported {args.csv}: {added} added, {updated} updated.")
    return 0


def _cmd_companies_list(args) -> int:
    companies = list_companies(connect(args.db), active_only=args.active)
    if args.json:
        print(json.dumps(companies, indent=2, ensure_ascii=False))
        return 0
    rows = []
    for c in companies:
        boards = c["boards"]
        successes = [b["last_success_at"] for b in boards if b["last_success_at"]]
        errors = [f"{b['source']}/{b['ref']}: {b['last_error']}" for b in boards if b["last_error"]]
        rows.append([
            c["name"], c["tier"], "yes" if c["active"] else "no",
            ", ".join(f"{b['source']}/{b['ref']}" for b in boards) or "-",
            c["open_roles"] if boards else "",
            max(successes)[:16].replace("T", " ") if successes else "",
            "; ".join(errors), ", ".join(c["tags"])])
    print(format_table(
        ["Company", "Tier", "Active", "Boards", "Open", "Last success (UTC)", "Last error", "Tags"],
        rows, {"Last error": 50}))
    mapped = sum(1 for c in companies if c["boards"])
    print(f"\n{len(companies)} companies, {mapped} mapped to a board.")
    return 0


def _cmd_companies_add(args) -> int:
    conn = connect(args.db)
    add_company(conn, args.name, website=args.website, tier=args.tier,
                tags=args.tags.split(",") if args.tags else None)
    print(f"Added {args.name.strip()}.")
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
    print(f"{get_company(conn, args.name)['name']} -> {args.source}/{args.ref.strip()}")
    return 0


def _cmd_companies_unmap(args) -> int:
    conn = connect(args.db)
    closed = remove_board(conn, args.name, args.source, args.ref)
    print(f"Removed {args.source}/{args.ref.strip()} from {get_company(conn, args.name)['name']}; "
          f"{closed} open role{'' if closed == 1 else 's'} closed.")
    return 0


# --- resolve and report -----------------------------------------------------

def _print_resolution(res: Resolution, dry_run: bool) -> None:
    verb = "would save" if dry_run else "saved"
    if res.outcome == "mapped":
        boards = ", ".join(f"{c.source}/{c.ref}" for c in res.to_save)
        line = f"{verb} {boards}"
    elif res.outcome == "needs_review":
        line = "needs review"
    elif res.outcome == "unsupported":
        line = f"unsupported: {res.job_system} ({res.job_system_url})"
    else:
        line = "unresolved"
    print(f"  {res.company} ... {line}", flush=True)


def _candidate_rows(results: list[Resolution], wanted) -> list[list]:
    return [[r.company, f"{c.source}/{c.ref}", c.kind, c.roles, c.detail]
            for r in results for c in r.candidates if wanted(r, c)]


def _cmd_companies_resolve(args) -> int:
    conn = connect(args.db)
    if not args.json:
        print(f"Database: {resolve_db_path(args.db)}" + ("  (dry run: nothing is saved)" if args.dry_run else ""))
    results = resolve_companies(
        conn, names=args.names, dry_run=args.dry_run, delay=args.delay,
        on_company=None if args.json else lambda r: _print_resolution(r, args.dry_run))
    if args.json:
        print(json.dumps([{**vars(r), "candidates": [vars(c) for c in r.candidates]}
                          for r in results], indent=2, ensure_ascii=False))
        return 0
    if not results:
        print("Nothing to resolve: every active company already has a board.")
        return 0

    verb = "Would save" if args.dry_run else "Saved"
    headers = ["Company", "Board", "Found by", "Roles", "Evidence"]
    widths = {"Evidence": 130}
    saved = _candidate_rows(results, lambda r, c: c in r.to_save)
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
    unsupported = [[r.company, r.job_system, r.job_system_url] for r in results
                   if r.outcome == "unsupported"]
    if unsupported:
        print(f"\nJob system detected but not supported ({len(unsupported)}):")
        print(format_table(["Company", "Job system", "URL"], unsupported, {"URL": 100}))
    unresolved = [[r.company, "; ".join(r.notes) or "no careers page or board found"]
                  for r in results if r.outcome == "unresolved"]
    if unresolved:
        print(f"\nUnresolved ({len(unresolved)}):")
        print(format_table(["Company", "What happened"], unresolved, {"What happened": 110}))
    counts = {o: sum(r.outcome == o for r in results)
              for o in ("mapped", "needs_review", "unsupported", "unresolved")}
    print(f"\n{len(results)} companies: {counts['mapped']} mapped, {counts['needs_review']} need review, "
          f"{counts['unsupported']} on an unsupported system, {counts['unresolved']} unresolved. "
          f"{sum(r.requests for r in results)} requests.")
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
        detail = "; ".join(f"{c['source']}/{c['ref']}: {c['detail']}" for c in pending) or u["reason"]
        rows.append([u["company"], "needs review" if pending else u["reason"].split(";")[0][:40], detail])
    print(format_table(["Company", "Status", "Detail"], rows, {"Detail": 100}) if rows else "  (none)")
    return 0


# --- scout ------------------------------------------------------------------

def _print_progress(result: BoardResult) -> None:
    if result.status == "ok":
        detail = f"{result.roles_fetched} roles" + (" (baseline)" if result.baseline else "")
    else:
        detail = f"FAILED: {result.error}"
    print(f"  {result.company} ({result.source}/{result.ref}) ... {detail}", flush=True)


def _cmd_scout(args) -> int:
    conn = connect(args.db)
    if not args.json:
        print(f"Database: {resolve_db_path(args.db)}")
    summary = run_scout(conn, delay=args.delay, on_board=None if args.json else _print_progress)

    if args.json:
        print(json.dumps({
            "run_id": summary.run_id, "started_at": summary.started_at,
            "finished_at": summary.finished_at, "status": summary.status,
            "boards_ok": summary.ok, "boards_failed": summary.failed,
            "unmapped_skipped": summary.unmapped,
            "boards": [vars(b) for b in summary.boards],
            "companies": [vars(s) for s in summary.snapshots],
        }, indent=2, ensure_ascii=False))
    else:
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
                    "; ".join(b.error or "" for b in failed)])
            rows.append(["TOTAL", f"{len(summary.boards)} boards",
                         f"{summary.ok} ok, {summary.failed} failed",
                         sum(b.roles_fetched or 0 for b in summary.boards),
                         summary.total("open_roles"), summary.total("opened"),
                         summary.total("closed"), ""])
            print(format_table(
                ["Company", "Boards", "Status", "Fetched", "Open", "Opened", "Closed", "Error"],
                rows, {"Error": 60}))
        else:
            print("No active company is mapped to a board. Use: rolescout companies resolve "
                  "(or: rolescout companies map NAME SOURCE REF)")
        if summary.unmapped:
            print(f"\nSkipped {summary.unmapped} active companies with no board mapping.")
    # Non-zero only when nothing could be fetched at all (e.g. no network).
    return 1 if summary.boards and summary.ok == 0 else 0


# --- query ------------------------------------------------------------------

def _cmd_query(args) -> int:
    conn = connect(args.db)
    if args.company:
        get_company(conn, args.company)  # a typo should fail loudly, not match nothing
    try:
        roles = query_roles(conn, company=args.company, title=args.title, status=args.status,
                            since=args.since, include_inactive=args.include_inactive,
                            limit=args.limit)
    except ValueError:
        print(f"error: --since must be a date like 2026-01-31, got {args.since!r}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(roles, indent=2, ensure_ascii=False))
        return 0
    headers = ["Company", "Title", "Location", "Posted", "First seen", "Status", "URL"]
    rows = [[r["company"], r["title"], r["location"], r["date_posted"], r["first_seen_at"][:10],
             r["status"] if r["status"] == "open" else f"closed {r['closed_at'][:10]}", r["url"]]
            for r in roles]
    print(format_table(headers, rows, {"Company": 20, "Title": 55, "Location": 30}))
    print(f"\n{len(roles)} role{'' if len(roles) == 1 else 's'}.")
    return 0


# --- parser -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rolescout", description=__doc__)
    parser.add_argument("--version", action="version", version=f"rolescout {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    db = argparse.ArgumentParser(add_help=False)
    db.add_argument("--db", metavar="PATH",
                    help=f"database file (default: ${DB_ENV_VAR}, else {DEFAULT_DB_PATH})")

    fetch = commands.add_parser(
        "fetch", help="fetch one board live and print the role count and a sample role")
    fetch.add_argument("source", choices=sorted(ADAPTERS))
    fetch.add_argument("slug", help="board slug, e.g. the last part of jobs.lever.co/<slug>")
    fetch.add_argument("--sample", type=int, default=1, metavar="N",
                       help="which role to print, 1-based (default: 1)")
    fetch.add_argument("--full", action="store_true", help="print the whole description")
    fetch.set_defaults(func=_cmd_fetch)

    companies = commands.add_parser("companies", help="manage target companies and board mappings")
    sub = companies.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")

    p = sub.add_parser("import", parents=[db],
                       help="import a CSV with company_name, website, tier columns")
    p.add_argument("csv")
    p.set_defaults(func=_cmd_companies_import)

    p = sub.add_parser("list", parents=[db], help="list companies with their board and status")
    p.add_argument("--active", action="store_true", help="only active companies")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_companies_list)

    p = sub.add_parser("add", parents=[db], help="add one company")
    p.add_argument("name")
    p.add_argument("--website", default="")
    p.add_argument("--tier", type=int)
    p.add_argument("--tags", help="comma-separated, e.g. ai,fintech")
    p.set_defaults(func=_cmd_companies_add)

    p = sub.add_parser("deactivate", parents=[db], help="stop scouting a company (keeps its history)")
    p.add_argument("name")
    p.set_defaults(func=_cmd_companies_set_active, active=False)

    p = sub.add_parser("activate", parents=[db], help="resume scouting a deactivated company")
    p.add_argument("name")
    p.set_defaults(func=_cmd_companies_set_active, active=True)

    p = sub.add_parser("map", parents=[db],
                       help="add a board to a company, e.g. map Anthropic greenhouse anthropic")
    p.add_argument("name")
    p.add_argument("source", choices=sorted(ADAPTERS))
    p.add_argument("ref", help="board reference (slug)")
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
    p.add_argument("--delay", type=float, default=1.0, metavar="SECONDS",
                   help="minimum pause between requests (default: 1.0)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_companies_resolve)

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
    query.add_argument("--json", action="store_true")
    query.set_defaults(func=_cmd_query, status="open")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CompanyError, SchemaError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
