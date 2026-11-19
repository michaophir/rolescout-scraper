# RoleScout

`rolescout` is a local Python command-line tool. Once a day it fetches every
open role from the job boards of the companies you track, stores them in a
SQLite database on your machine, and ranks a shortlist. There is no hosted
service. An optional MCP server lets an agent read and update the same
database.

User docs: README.md (overview), DEPLOY.md (install, schedule, every
feature), MCP.md (the MCP server).

---

## Setup and tests

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Python 3.11 or newer. Runtime dependencies are `requests` and
`beautifulsoup4` only; the MCP SDK is an optional extra. Do not add a runtime
dependency without asking.

Tests are offline: every HTTP exchange comes from `tests/fixtures` or is
written in the test. `tests/refresh_fixtures.py` is the only script that
touches live boards, and it is run by hand.

---

## Layout

| Path | What it holds |
|---|---|
| `src/rolescout/cli.py` | The `rolescout` command (argparse); every subcommand |
| `src/rolescout/adapters/` | One module per source behind `SourceAdapter` in `base.py` |
| `src/rolescout/robots.py` | robots.txt evaluated as RFC 9309 says; used by every caller |
| `src/rolescout/resolve.py` | Finds a company's board from its website, with evidence |
| `src/rolescout/scout.py` | The scout run: fetch, store, open and close roles |
| `src/rolescout/shortlist.py`, `filters.py` | Ranking from `ranking.toml`, and cuts by profile and location |
| `src/rolescout/companies.py`, `profiles.py`, `vcs.py`, `overview.py` | Companies, their profiles, the VC list |
| `src/rolescout/portfolio.py`, `candidates.py` | Portfolio boards as a source of new companies |
| `src/rolescout/gaps.py`, `trends.py`, `digest.py`, `query.py` | Reports from the database, no network |
| `src/rolescout/ops.py`, `schedule.py`, `jobs.py` | Status, backups, the launchd schedule, what is running now |
| `src/rolescout/db.py` | SQLite connection and the `MIGRATIONS` list |
| `src/rolescout/mcp_server.py` | The MCP server (`rolescout serve`) |
| `src/rolescout/transfer.py` | Export and import of companies between machines |

---

## Sources

| Source | Reference | Notes |
|---|---|---|
| `greenhouse`, `lever`, `ashby`, `gem`, `workable` | board name | Whole board with descriptions in one request |
| `rippling` | board name | List in one request; description, date and pay cost one request per role |
| `workday` | `TENANT/wdN/SITE` | 20 roles per request; one request per description |
| `eightfold` | `SITE-HOST/COMPANY-DOMAIN` | Ten roles per request; two list endpoints, the second tried when the first cannot be read; no descriptions |
| `google` | `careers` | The single-company exception below: one feed, one request |

Sources that charge a request per description fetch it only for roles whose
title the ranking config wants and for roles marked saved or applied.

---

## Rules the code keeps

- Politeness: at most one request per second per host, an honest
  User-Agent, no headless browser, no login. The Getro API is never called.
- robots.txt is read before anything else on a host and evaluated by
  `rolescout.robots`: longest matching rule wins, `Allow` wins a tie, `*`
  and `$` are wildcards. A missing file allows everything; a server error
  disallows everything. Never use the standard library's parser for this.
- Closing: a role closes on its second miss in a row, and only complete
  reads count a miss. A failed fetch changes nothing.
- Completeness: a source that reports a total counts as complete when it saw
  at least 99% of it (`COMPLETE_SHARE` in `adapters/base.py`). Seen and
  reported are stored per fetch in `run_boards`.
- Schema: version 10, kept in `PRAGMA user_version`. A change is a new entry
  at the end of `MIGRATIONS` in `db.py`; existing databases upgrade in place
  when any command opens them. Never edit an earlier entry.
- Personal files live next to the database in `~/.rolescout`, not in the
  repo: `ranking.toml`, `candidates.toml`, `sectors.toml` and company
  exports. `.gitignore` names them.

---

## Conventions

- Adapters are built per job system by default. A single-company adapter is
  allowed only when that company runs its own career site with no shared job
  system behind it, and it is named as an exception in CLAUDE.md with the
  reason. Company-specific branches inside a shared adapter are not allowed.
- Exception: `src/rolescout/adapters/google.py` reads Google's career site.
  Reason: Google runs its own site with no shared job system behind it, and
  publishes every open role in one public feed that robots.txt allows. The
  company's name appears in that adapter, its tests, its fixture and the
  docs that describe it, and nowhere else.
- No real company names in code, comments, docstrings, error messages, tool
  descriptions, CLI help, docs or tests. Use a neutral example such as
  `acme/wd5/Careers`.
- Target companies are personal data. They live in the database, not in the
  repo.
- Fixtures are recorded from real boards and then anonymized, and are named
  after the behavior they test.

---

## Website demo files

The web app at https://getrolescout.com loads these six files at the repo
root from raw GitHub URLs on `main`, as demo data. `rolescout` itself reads
none of them. Do not move, rename or edit them while the website loads them:

`candidate_profile.json`, `open_roles.csv`, `tracking_sheet.csv`,
`last_run_summary.json`, `target_company_list.csv`, `role_filters.csv`

---

## Notes

- Version 1 (a single script) is at the git tag `v1.0`.
- Use `python3`, not `python`.
