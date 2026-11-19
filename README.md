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
rolescout init                           # starter companies, their boards, starter configs
rolescout scout                          # fetch every mapped board
rolescout shortlist                      # the roles to look at first
```

`rolescout init` gives a new install something useful to run at once; the
next section says what it sets up and how to change it. The first scout
takes about 20 minutes, most of it on the few boards that are read page
by page. The database is `~/.rolescout/rolescout.db`, and the two config
files live next to it. All three are yours and are not part of this
repository.

To start from an empty list instead, run `rolescout init --no-companies`,
then add companies and let rolescout find their boards:

```sh
rolescout companies add "Acme" --website acme.com
rolescout companies resolve              # find each company's job board
rolescout companies report               # mapped, unsupported, unresolved
```

## Defaults and how to change them

What ships is a starting point for someone looking for product or
engineering roles at technology companies. Nothing in it is a
recommendation, and all of it is meant to be edited.

| What | What ships | Where it lives after `init` |
|---|---|---|
| Companies | 150 well-known technology companies, each with its website and a job board that was confirmed live | the database |
| Ranking | Product and engineering titles, all worth the same; seniority words, all worth the same; interns, new graduates and apprentices excluded; no location preference | `~/.rolescout/ranking.toml` |
| Candidate filter | Broad technology sectors; every stage and size accepted; no location rule | `~/.rolescout/candidates.toml` |
| Portfolio boards | None, unless you ask: `rolescout init --portfolio` registers 5 public investor job boards as an example | the database |
| Sectors | Built-in rules that sort raw tags into sectors | `~/.rolescout/sectors.toml`, once you create it |

The starter companies by job system: Greenhouse 92, Ashby 39, Workday 10, Lever 5, and one each on
Rippling, Workable, Eightfold and Google's own career site. None is on Gem: no
Gem board could be confirmed for a company on the list. The largest Workday
and Eightfold boards were left out so that a first run stays short; add them
with `rolescout companies map` if you want them.

**Companies.** The starter list is only a list of well-known names.

```sh
rolescout companies list
rolescout companies add "Acme" --website acme.com      # then: rolescout companies resolve "Acme"
rolescout companies map "Acme" greenhouse acme         # or map a board by hand
rolescout companies deactivate "Globex"                # stop fetching it; history is kept
rolescout companies import my-companies.csv            # columns: company_name, website
```

Running `rolescout init` again adds starter companies you do not have and
leaves every company you already have exactly as it is.

**Ranking and filters.** Open `~/.rolescout/ranking.toml`. Each section
says what it does:

- `[title.wanted]`: the title phrases a role must match, with points. Delete
  the half you do not want, and raise the points of the titles you want most.
- `[title] exclude`: phrases that keep a role off the list.
- `[seniority.words]`: points for seniority words in the title.
- `[location]`: no preference to begin with. The file carries commented
  examples for requiring a place (`require = ["United States"]`), for points
  per place (`"New York" = 10`) and for points when a role is remote.
- `[leveling]`: companies where a Senior title counts as Staff; empty to
  begin with, with an example.

Then run `rolescout shortlist rebuild` (no network), and `rolescout why ID`
to see how any role was scored.

**Sectors.** `rolescout sectors init` writes `sectors.toml` from the tags in
your own data; edit it and run `rolescout sectors apply`.

**Portfolio boards and VCs.** These suggest companies you do not track yet.

```sh
rolescout portfolio add https://jobs.example.vc        # your own choice of boards
rolescout portfolio list
rolescout portfolio remove https://jobs.example.vc
rolescout portfolio harvest                            # read them, apply candidates.toml
rolescout candidates --status eligible
```

`~/.rolescout/candidates.toml` decides which of the companies found are
worth tracking; it has commented examples for a minimum size, stages to
reject and a location rule. `rolescout vcs list` shows the investors behind
the boards.

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
