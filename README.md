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

## Quick start

From a clone to a ranked shortlist takes well under an hour, most of it the
first fetch. You need Python 3.11 or newer (`python3 --version`); macOS or
Linux.

**1. Install**

```sh
git clone https://github.com/michaophir/rolescout-scraper.git ~/rolescout-scraper
python3 -m venv ~/.rolescout/venv
~/.rolescout/venv/bin/pip install ~/rolescout-scraper
mkdir -p ~/.local/bin && ln -sf ~/.rolescout/venv/bin/rolescout ~/.local/bin/rolescout
export PATH="$HOME/.local/bin:$PATH"
rolescout --version
```

**2. Set up the starter companies and configs**

```sh
rolescout init
```

This adds 150 well-known technology companies with their job boards and
writes a starter ranking. [Defaults and how to change them](#defaults-and-how-to-change-them)
says what you get.

**3. Fetch the roles** (about 20 minutes with the starter set)

```sh
rolescout scout
```

**4. Read the shortlist**

```sh
rolescout shortlist                      # the roles to look at first
rolescout why 123                        # how one role was scored
rolescout mark 123 saved                 # or dismissed, applied
rolescout query --title designer --location remote
rolescout query --text kubernetes        # search the descriptions
```

**5. Run it every day** (macOS; on Linux the command prints a cron line)

```sh
rolescout schedule install --at 07:30
rolescout status                         # health, last run, what is running now
rolescout digest                         # what changed since the previous scout
```

**6. Connect an agent** (optional)

```sh
~/.rolescout/venv/bin/pip install "$HOME/rolescout-scraper[mcp]"
claude mcp add --transport stdio --scope user rolescout -- ~/.rolescout/venv/bin/rolescout serve
```

The second line is for Claude Code on the same machine; [MCP.md](MCP.md) has
the other clients, remote access and the list of tools.

To start from an empty list instead of the starter companies, run
`rolescout init --no-companies`, then `rolescout companies add "Acme"
--website acme.com` and `rolescout companies resolve` to find each board.

## Where to read more

| For | Read |
|---|---|
| An always-on Mac: install, schedule, backups, updates, moving a list between machines | [DEPLOY.md](DEPLOY.md), steps 1 to 9 |
| How a feature works: ranking, filters, profiles and VCs, portfolio boards, each job system, robots.txt, partial reads | [DEPLOY.md, Reference](DEPLOY.md#reference) |
| Something went wrong | [DEPLOY.md, Troubleshooting](DEPLOY.md#troubleshooting) |
| Agents over MCP | [MCP.md](MCP.md) |
| Working on the code | [CLAUDE.md](CLAUDE.md) |

Every command also explains itself: `rolescout --help`, `rolescout COMMAND --help`.

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

**Ranking and filters.** Open `~/.rolescout/ranking.toml`; each section in
the file says what it does. Delete the titles you do not want under
`[title.wanted]` and raise the points of the ones you want most; add
phrases to `exclude`; and use the commented examples to require or prefer a
location, give points for remote roles, or name companies whose Senior
counts as Staff. Then run `rolescout shortlist rebuild` (no network).
[DEPLOY.md](DEPLOY.md#the-shortlist-ranking) has the details.

**Sectors.** `rolescout sectors init` writes `sectors.toml` from the tags in
your own data; edit it and run `rolescout sectors apply`
([details](DEPLOY.md#company-profiles-sectors-and-vcs)).

**Portfolio boards and VCs.** These suggest companies you do not track yet.
`~/.rolescout/candidates.toml` decides which of the companies found are
worth tracking; it has commented examples for a minimum size, stages to
reject and a location rule ([details](DEPLOY.md#new-companies-from-portfolio-boards)).

```sh
rolescout portfolio add https://jobs.example.vc        # your own choice of boards
rolescout portfolio remove https://jobs.example.vc
rolescout portfolio harvest                            # read them, apply candidates.toml
rolescout candidates --status eligible
```

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The tests run offline against recorded, anonymized responses. Layout, rules
and conventions are in [CLAUDE.md](CLAUDE.md).

## Website demo data

The six data files at the repository root (`candidate_profile.json`,
`open_roles.csv`, `tracking_sheet.csv`, `last_run_summary.json`,
`target_company_list.csv`, `role_filters.csv`) are demo data that the web app
at https://getrolescout.com loads from this repository. `rolescout` does not
read them.
