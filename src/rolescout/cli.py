"""The ``rolescout`` console command."""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from rolescout import __version__
from rolescout.adapters import ADAPTERS, SourceError, get_adapter
from rolescout.companies import (
    CompanyError, add_board, add_company, get_company, import_csv, list_companies, remove_board,
    set_active,
)
from rolescout.db import DB_ENV_VAR, DEFAULT_DB_PATH, SchemaError, connect, resolve_db_path
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
from rolescout.transfer import TransferError, export_companies, import_companies

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
    print(f"Exported {len(data['companies'])} companies and {boards} boards to {path}")
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
        line = (f"uses {res.job_system}, board name not found" if res.job_system and not res.candidates
                else "needs review")
    elif res.outcome == "unsupported":
        line = f"unsupported: {res.job_system} ({res.job_system_url})"
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
    results = resolve_companies(
        conn, names=None if recheck else args.names, failing=failing, dry_run=args.dry_run,
        delay=args.delay, on_company=None if args.json else lambda r: _print_resolution(r, args.dry_run))
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

def _tier(tier) -> str:
    return "" if tier is None else f"T{tier}"


def _print_gaps(report: dict) -> None:
    uncovered = report["active_companies"] - report["covered"]
    print(f"Coverage gaps: {summary_line(report)} "
          f"({uncovered} of {report['active_companies']} active companies not covered)")

    def system_rows(groups):
        return [[g["system"] if i == 0 else "", len(g["companies"]) if i == 0 else "",
                 g["why"] if i == 0 else "", m["company"], _tier(m["tier"]),
                 (m["url"] or "") + ("  (by hand)" if m["manual"] else "")]
                for g in groups for i, m in enumerate(g["companies"])]
    headers = ["Job system", "Count", "Why", "Company", "Tier", "URL"]
    candidates = [g for g in report["systems"] if g["build_candidate"]]
    others = [g for g in report["systems"] if not g["build_candidate"]]
    print("\nBuild candidates (3 or more companies, or a tier-1 company) "
          "- details: rolescout brief SYSTEM")
    print(format_table(headers, system_rows(candidates), {"URL": 80}) if candidates else "  (none)")
    print("\nOther unsupported job systems:")
    print(format_table(headers, system_rows(others), {"URL": 80}) if others else "  (none)")

    print("\nOn a supported system, not mapped yet - rolescout companies map NAME SOURCE BOARD")
    rows = []
    for e in report["needs_mapping"]:
        boards = "; ".join(f"{c['source']}/{c['ref']}: {c['detail']}" for c in e["candidates"])
        rows.append([e["company"], _tier(e["tier"]), e["system"] or "",
                     boards or (e["notes"][-1] if e["notes"] else "")])
    print(format_table(["Company", "Tier", "System", "Detail"], rows, {"Detail": 110}) if rows else "  (none)")

    print("\nNeeds identification - rolescout companies set-system NAME SYSTEM URL")
    rows = [[e["company"], _tier(e["tier"]), e["careers_url"] or e["website"] or "", e["reason"]]
            for e in report["unidentified"]]
    print(format_table(["Company", "Tier", "Careers page", "Why"], rows, {"Why": 80}) if rows else "  (none)")


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

def _print_progress(result: BoardResult) -> None:
    if result.status == "ok":
        detail = f"{result.roles_fetched} roles" + (" (baseline)" if result.baseline else "")
    else:
        detail = f"FAILED: {result.error}"
    print(f"  {result.company} ({result.source}/{result.ref}) ... {detail}", flush=True)


def _scout_once(conn, args, *, as_json: bool = False):
    """Run one scout and print its summary. Returns the summary."""
    summary = run_scout(conn, delay=args.delay, on_board=None if as_json else _print_progress)

    if as_json:
        print(json.dumps({
            "run_id": summary.run_id, "started_at": summary.started_at,
            "finished_at": summary.finished_at, "status": summary.status,
            "boards_ok": summary.ok, "boards_failed": summary.failed,
            "unmapped_skipped": summary.unmapped,
            "boards": [vars(b) for b in summary.boards],
            "companies": [vars(s) for s in summary.snapshots],
        }, indent=2, ensure_ascii=False))
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
    return summary


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
        results = resolve_companies(conn, failing=failing, delay=args.delay,
                                    on_company=lambda r: _print_resolution(r, False))
        mark_rechecked(conn)
        if results:
            _print_resolve_summary(results, False)
        else:
            print("Nothing to re-check.")
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
        for attempt in range(1, args.attempts + 1):
            try:
                summary = _scout_once(conn, args)
            except ScoutAlreadyRunning as exc:
                print(f"Not starting: {exc}. Skipping this run.")
                return 0
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
        if code == 0 and recheck_due(conn, args.recheck_days):
            _weekly_recheck(conn, args)
        if args.backup_dir:
            _print_backup(conn, args.backup_dir, args.keep)
    except Exception as exc:  # the log must say why an unattended run died
        print(f"error: {type(exc).__name__}: {exc}")
        code = 1
    print(f"=== finished {datetime.now().astimezone().strftime('%H:%M:%S')}, "
          f"{'ok' if code == 0 else 'FAILED'} ===\n")
    return code


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
    if status["scout_running"]:
        lines.append(("Right now", "a scout is running"))
    failing = status["failing_boards"]
    lines.append(("Failing boards", f"{len(failing)} failing more than two runs in a row"
                  if failing else "none failing more than two runs in a row"))
    lines.append(("Open roles", f"{status['open_roles']:,} across {status['companies_mapped']} mapped "
                                f"companies ({status['companies_active']} active)"))
    lines.append(("Database", f"{status['database']} ({_human_bytes(status['database_bytes'])})"))
    lines.append(("Coverage gaps", status["gaps"]["summary"] + "  (rolescout gaps)"))
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

    p = sub.add_parser(
        "import", parents=[db],
        help="import companies: a CSV (company_name, website, tier) or a .json export with boards")
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
        "recheck", parents=[db],
        help="resolve unmapped companies and companies whose board keeps failing "
             "(the daily job does this weekly)")
    p.add_argument("--dry-run", action="store_true", help="show what would be saved; write nothing")
    p.add_argument("--delay", type=float, default=1.0, metavar="SECONDS")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_companies_resolve, recheck=True, names=[])

    p = sub.add_parser(
        "set-system", parents=[db],
        help="record by hand which job system a company uses; detection never overwrites it")
    p.add_argument("name")
    p.add_argument("system", help="e.g. workday, rippling, paycom, or custom for a home-built site")
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
        help="health at a glance, no network: last run, failures, open roles, next run")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_status)

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
    query.add_argument("--json", action="store_true")
    query.set_defaults(func=_cmd_query, status="open")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CompanyError, SchemaError, FileNotFoundError, TransferError, BackupError,
            launchd.ScheduleError, GapsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
