# RoleScout

RoleScout is a job-search tool that runs on your own machine. Once a day it
fetches every open role from the job boards of the companies you track,
stores them in a local SQLite database, and ranks a short list of the roles
to look at first. Nothing is hosted: your company list, your ranking and
your marks stay on your machine.

This is version 2.0. Version 1, a single script, is at the git tag `v1.0`.

## What it does

- **Fetches** open roles from Greenhouse, Lever, Ashby, Workday, Rippling,
  Gem, Workable and Eightfold boards, and from one company career site that
  runs on none of them.
- **Finds the board** for a company from its website
  (`rolescout companies resolve`), and reports the companies it cannot fetch
  yet, grouped by job system (`rolescout gaps`).
- **Tracks changes**: which roles opened and closed, per company and per
  week. A role closes only after it is missing from two complete reads in a
  row.
- **Ranks a shortlist** from a config file of the titles, seniority and
  locations you want, and explains any role's rank (`rolescout why ID`).
- **Keeps company profiles**: sectors, stage, size, HQ and investors, which
  the shortlist and the role search can filter on.
- **Suggests new companies** from investor portfolio job boards, filtered by
  a config file of your own.
- **Serves agents**: an optional MCP server over the same database.

It is polite by design: at most one request per second per site, an honest
User-Agent, robots.txt obeyed as RFC 9309 defines it, no headless browser
and no login.

## Install

Python 3.11 or newer.

```sh
git clone https://github.com/michaophir/rolescout-scraper.git
cd rolescout-scraper
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/rolescout --version
```

[DEPLOY.md](DEPLOY.md) is the full guide for an always-on Mac: install,
seeding the company list, the schedule, backups, updates and every feature
in detail.

## First steps

```sh
rolescout companies add "Acme" --website acme.com
rolescout companies resolve              # find each company's job board
rolescout companies report               # mapped, unsupported, unresolved
rolescout shortlist init                 # starter ranking config; edit it
rolescout scout                          # fetch every mapped board
rolescout shortlist
```

A board can also be mapped by hand, for example
`rolescout companies map "Acme" greenhouse acme`. The database is
`~/.rolescout/rolescout.db`; the ranking config `ranking.toml` lives next to
it. Both are personal data and are not part of this repository.

## The daily run

```sh
rolescout schedule install --at 07:30    # macOS; on Linux it prints a cron line
rolescout status                         # health, last run, what is running now
rolescout digest                         # what changed since the previous scout
```

Each day the schedule runs the scout, retries if the machine was offline,
and writes a backup. About once a week it also re-checks companies it could
not fetch and reads the portfolio boards. `rolescout status` exits 0 when
healthy and 1 when something needs attention, and lists any board whose last
read saw less than 99% of the roles the site reports.

Day to day:

```sh
rolescout shortlist --new                # roles new since the previous list
rolescout query --title designer --location remote
rolescout company "Acme"
rolescout mark 123 saved                 # or dismissed, applied
```

## Agents (MCP)

`rolescout serve` exposes the database to an AI agent over the Model Context
Protocol: the shortlist, role search, marks, companies, profiles, coverage
gaps and health. It makes no network request of its own. It needs one extra
install step:

```sh
.venv/bin/pip install ".[mcp]"
```

[MCP.md](MCP.md) covers starting it, the tools, read-only mode and client
configuration.

## Development

```sh
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The tests run offline against recorded, anonymized responses. Conventions
for contributors are in [CLAUDE.md](CLAUDE.md).

## Website demo data

The six data files at the repository root (`candidate_profile.json`,
`open_roles.csv`, `tracking_sheet.csv`, `last_run_summary.json`,
`target_company_list.csv`, `role_filters.csv`) are demo data that the web app
at https://getrolescout.com loads from this repository. `rolescout` does not
read them.
