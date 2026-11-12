"""The ``rolescout`` console command."""

import argparse
import json
import sys

from rolescout import __version__
from rolescout.adapters import ADAPTERS, SourceError, get_adapter
from rolescout.companies import (
    CompanyError, add_company, get_company, import_csv, list_companies, set_active, set_mapping,
)
from rolescout.db import DB_ENV_VAR, DEFAULT_DB_PATH, SchemaError, connect, resolve_db_path
from rolescout.query import query_roles
from rolescout.scout import CompanyResult, run_scout

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
    rows = [[c["name"], c["tier"], "yes" if c["active"] else "no",
             f"{c['source']}/{c['ref']}" if c["source"] else "-",
             c["open_roles"] if c["source"] else "",
             (c["last_success_at"] or "")[:16].replace("T", " "),
             c["last_error"] or "", ", ".join(c["tags"])] for c in companies]
    print(format_table(
        ["Company", "Tier", "Active", "Board", "Open", "Last success (UTC)", "Last error", "Tags"],
        rows, {"Last error": 50}))
    mapped = sum(1 for c in companies if c["source"])
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
    set_mapping(conn, args.name, args.source, args.ref)
    print(f"{get_company(conn, args.name)['name']} -> {args.source}/{args.ref.strip()}")
    return 0


# --- scout ------------------------------------------------------------------

def _print_progress(result: CompanyResult) -> None:
    if result.status == "ok":
        detail = f"{result.roles_fetched} roles"
    else:
        detail = f"FAILED: {result.error}"
    print(f"  {result.company} ({result.source}/{result.ref}) ... {detail}", flush=True)


def _cmd_scout(args) -> int:
    conn = connect(args.db)
    print(f"Database: {resolve_db_path(args.db)}")
    summary = run_scout(conn, delay=args.delay, on_company=None if args.json else _print_progress)

    if args.json:
        print(json.dumps({
            "run_id": summary.run_id, "started_at": summary.started_at,
            "finished_at": summary.finished_at, "status": summary.status,
            "companies_ok": summary.ok, "companies_failed": summary.failed,
            "unmapped_skipped": summary.unmapped,
            "companies": [vars(c) for c in summary.companies],
        }, indent=2, ensure_ascii=False))
    else:
        print(f"\nRun {summary.run_id} {summary.status}: {summary.started_at} -> {summary.finished_at}")
        if summary.companies:
            rows = [[c.company, f"{c.source}/{c.ref}", c.status,
                     "" if c.roles_fetched is None else c.roles_fetched,
                     c.open_roles, c.opened, c.closed, c.error or ""] for c in summary.companies]
            rows.append(["TOTAL", "", f"{summary.ok} ok, {summary.failed} failed",
                         sum(c.roles_fetched or 0 for c in summary.companies),
                         summary.total("open_roles"), summary.total("opened"),
                         summary.total("closed"), ""])
            print(format_table(
                ["Company", "Board", "Status", "Fetched", "Open", "Opened", "Closed", "Error"],
                rows, {"Error": 60}))
        else:
            print("No active company is mapped to a board. "
                  "Use: rolescout companies map NAME SOURCE REF")
        if summary.unmapped:
            print(f"\nSkipped {summary.unmapped} active companies with no board mapping.")
    # Non-zero only when nothing could be fetched at all (e.g. no network).
    return 1 if summary.companies and summary.ok == 0 else 0


# --- query ------------------------------------------------------------------

def _cmd_query(args) -> int:
    conn = connect(args.db)
    if args.company:
        get_company(conn, args.company)  # a typo should fail loudly, not match nothing
    try:
        roles = query_roles(conn, company=args.company, title=args.title, status=args.status,
                            since=args.since, limit=args.limit)
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
                       help="set a company's source and board, e.g. map Anthropic greenhouse anthropic")
    p.add_argument("name")
    p.add_argument("source", choices=sorted(ADAPTERS))
    p.add_argument("ref", help="board reference (slug)")
    p.set_defaults(func=_cmd_companies_map)

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
