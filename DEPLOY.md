# Deploying rolescout on an always-on Mac

rolescout runs once a day, fetches every open role from the mapped job boards,
and stores them in a local SQLite database. This guide installs it on a fresh
Mac, seeds the company list, schedules the daily run and shows how to check
on it. Every step is a command to run in Terminal (or over SSH) on **the Mac
that will run it**, unless the step says "on your own machine".

Scheduling uses launchd and works on macOS only. On Linux, `rolescout schedule
install` refuses and prints an equivalent cron line instead; everything else
(scout, status, backup, export, import) works the same.

## Where things live

| What | Path |
|---|---|
| Code | `~/rolescout-scraper` (git clone) |
| Python environment | `~/.rolescout/venv` |
| Database | `~/.rolescout/rolescout.db` |
| Backups | `~/.rolescout/backups/rolescout-YYYYMMDD-HHMMSS.db` (last 7) |
| Log | `~/Library/Logs/rolescout/scout.log` (rotated at 1 MB, 3 old files kept) |
| Schedule | `~/Library/LaunchAgents/com.getrolescout.scout.plist` |

## 1. Check Python

rolescout needs Python 3.11 or newer. The `python3` that ships with macOS is
older.

```sh
python3 --version
```

If it prints 3.11 or higher, continue. If not, install a current Python from
https://www.python.org/downloads/macos/ and use its full path (for example
`/Library/Frameworks/Python.framework/Versions/3.13/bin/python3`) wherever
this guide says `python3` in step 2. Stop here if no Python 3.11+ is available.

## 2. Install

```sh
git clone https://github.com/michaophir/rolescout-scraper.git ~/rolescout-scraper
python3 -m venv ~/.rolescout/venv
~/.rolescout/venv/bin/pip install ~/rolescout-scraper
```

Make the command available in every shell:

```sh
mkdir -p ~/.local/bin
ln -sf ~/.rolescout/venv/bin/rolescout ~/.local/bin/rolescout
grep -q '.local/bin' ~/.zshrc 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
export PATH="$HOME/.local/bin:$PATH"
```

Check: `rolescout --version` prints a version number.

## 3. Seed the companies and board mappings

The company list and its board mappings come from a machine where
`rolescout companies resolve` has already been run, so this Mac never needs
to resolve anything.

**On your own machine** (replace `mac` with the SSH name of the always-on Mac):

```sh
rolescout companies export - | ssh mac '~/.rolescout/venv/bin/rolescout companies import -'
```

Or as a file. The export lists the companies you are targeting: it is
personal data, so keep it out of the git repository.

```sh
rolescout companies export ~/rolescout-companies.json     # on your own machine
scp ~/rolescout-companies.json mac:                        # on your own machine
rolescout companies import ~/rolescout-companies.json      # on the Mac
rm ~/rolescout-companies.json                              # on both, when done
```

Check, on the Mac: `rolescout companies report` shows the same mapped,
unsupported and unresolved counts as on your own machine.

Importing again later is safe: it adds new companies and boards and updates
details, and never removes anything.

### The shortlist ranking

The shortlist is ranked by `~/.rolescout/ranking.toml`, a personal file that
lives next to the database and is not part of the repository. Without it the
scout still runs and simply builds no shortlist. Either copy yours across
(on your own machine):

```sh
scp ~/.rolescout/ranking.toml mac:.rolescout/ranking.toml
```

or create a starter on the Mac and edit it:

```sh
cd ~/rolescout-scraper && rolescout shortlist init
```

Two things the ranking does that are worth knowing when you read the list:
no company takes more than `max_per_company` slots (5 in the starter; roles
held back by that cap are counted in the by-company view and named by
`rolescout why ID`), and a role's age is counted from the date the company
posted it, falling back to the date rolescout first saw it. Roles that were
already open when a company was first fetched and carry no posted date get
no points for being new.

After changing the file, `rolescout shortlist rebuild` re-ranks at once, with
no network call. Marks (dismissed, saved, applied) are stored in the database
on the machine where you make them.

## 4. First run

Run one scout by hand so problems show up now, not at the scheduled time:

```sh
rolescout scout
```

It takes about one second per board. The first run is a baseline: roles are
stored but not counted as "opened". The run ends with a table and a TOTAL
line; `0 failed` is the expected result.

## 5. Schedule the daily run

Pick a time (24-hour, local time of the Mac):

```sh
rolescout schedule install --at 07:30
```

Each day at that time the Mac runs the scout and then writes a backup. If
the Mac was asleep at that time, the run happens when it wakes. If every
board fails (usually no network right after waking), it retries twice, two
minutes apart.

Two conditions for runs to happen:

- The Mac must be powered on. A run missed while it was shut down is skipped.
- The user who installed the schedule must be logged in on the Mac itself.
  For a Mac that restarts unattended, turn on automatic login (System
  Settings > Users & Groups > Automatically log in as). If the install
  command reports `launchd domain user/...` rather than `gui/...`, no desktop
  session was present: log in at the Mac's screen once, then run the install
  command again.

Running `schedule install` again with a different time replaces the schedule.

## 6. Verify

```sh
rolescout status
```

The first line is `HEALTHY` or `NEEDS ATTENTION` with the reasons listed
under it. It also shows the last run, companies ok and failed, boards failing
more than two runs in a row, open roles, database size, the last backup and
the next scheduled run. It makes no network call. The exit code is 0 when
healthy and 1 when not, so from your own machine:

```sh
ssh mac '~/.rolescout/venv/bin/rolescout status'
```

`status` also prints one `Coverage gaps:` line. Gaps are companies rolescout
cannot fetch yet; they never change the exit code. About once a week the
daily run re-checks them by itself: it re-resolves companies with no board
and companies whose board has failed three or more runs in a row. The re-check
runs after the scout and the backup, each company gets at most 60 seconds,
and the whole re-check stops after 15 minutes; companies it did not reach go
first the following week. To see the detail, or to record a job system found
by hand:

```sh
rolescout gaps
rolescout brief SYSTEM
rolescout companies set-system NAME SYSTEM URL
```

After the first scheduled time has passed, check that the run happened:

```sh
tail -40 ~/Library/Logs/rolescout/scout.log
ls -lh ~/.rolescout/backups
```

Each run in the log starts with `=== rolescout daily, <time> ===` and ends
with `=== finished <time>, ok ===`.

## Workday boards

A Workday board is named by three things that do not follow from the company
name: tenant, data center and career site, written `TENANT/wdN/SITE`
(for example `acme/wd5/Careers`). `rolescout companies resolve` maps
one whenever a company's site links to a Workday URL. To map one by hand,
paste any URL from the career site, a job page included:

```sh
rolescout companies map "Acme" workday "https://acme.wd5.myworkdayjobs.com/en-US/Careers/login"
```

Workday costs more requests than the other sources, so the scout prints the
requests and time each board took:

- The list of roles takes one request per 20 roles, one second apart.
- A description takes one request per role. It is fetched once, and only for
  roles whose title matches a wanted phrase in `ranking.toml` (and no excluded
  one) and for roles you marked saved or applied. Every other role is stored
  without a description. After you add a wanted phrase, the next scout fetches
  the descriptions it now needs.
- One board gets at most 150 descriptions, 900 requests and 15 minutes per
  run. What does not fit waits for the next run; a board cut short is marked
  `partial` and none of its roles are closed that day.
- A posted date comes from "Posted 5 Days Ago" in the list (exact once the
  description is fetched). "Posted 30+ Days Ago" gives no date.

## New companies from portfolio boards

Investor portfolio job boards can be read for companies you do not track
yet. They are never used for roles. The board list and the filter are
personal data: the boards live in the database and the filter in
`~/.rolescout/candidates.toml`, next to it.

```sh
rolescout portfolio add https://jobs.example.vc      # once per board
cd ~/rolescout-scraper && rolescout candidates init   # starter filter; edit it
rolescout portfolio harvest                           # read the boards, apply the filter
rolescout candidates --status eligible
rolescout candidates why NAME
rolescout candidates activate NAME                    # or --all, or --first N
```

Only boards on the Getro platform can be read, from their public pages; the
platform's API is off limits and is never called. A harvest makes at most one
request per second, obeys each board's robots.txt, and stops at its time
limit; the next one continues where it stopped. The daily job runs a harvest
once a week after the scout and the backup, for at most 15 minutes a day
(`--harvest-days`, `--harvest-minutes`), and nothing it does can fail the
scout. Activating a candidate adds it as a tier 3 company; its own job board
is found by the weekly re-check, or at once with `rolescout companies resolve`.
After editing the filter, `rolescout candidates apply` re-sorts every
candidate with no network call.

## Agents (MCP)

To let an AI agent read the shortlist and work with rolescout without the
command line, see [MCP.md](MCP.md). It needs one extra install step and is
optional: the scraper and the scheduled job do not depend on it.

## 7. Update to a new version

```sh
cd ~/rolescout-scraper && git pull
~/.rolescout/venv/bin/pip install --upgrade ~/rolescout-scraper
rolescout --version
rolescout status
```

The schedule keeps working: it points at `~/.rolescout/venv`, which is
updated in place. The database is upgraded automatically the next time any
command opens it. If an update is running while the scheduled scout starts,
that scout may fail once; `rolescout status` will show it and the next day's
run recovers.

To take a safety copy before updating: `rolescout backup ~/.rolescout/backups`.

## 8. Restore from a backup

```sh
rolescout schedule remove
cp ~/.rolescout/backups/rolescout-YYYYMMDD-HHMMSS.db ~/.rolescout/rolescout.db
rm -f ~/.rolescout/rolescout.db-wal ~/.rolescout/rolescout.db-shm
rolescout status
rolescout schedule install --at 07:30
```

## 9. Uninstall

```sh
rolescout schedule remove
rm -f ~/.local/bin/rolescout
rm -rf ~/.rolescout/venv ~/rolescout-scraper ~/Library/Logs/rolescout
```

This leaves the database and backups in `~/.rolescout`. To delete the data
too, which cannot be undone: `rm -rf ~/.rolescout`.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `status` says no run for N hours | Is the Mac on and the user logged in? Run `rolescout schedule install --at HH:MM` again, then `launchctl print gui/$(id -u)/com.getrolescout.scout`. |
| `status` says the last run could not fetch any board | The Mac had no network. Run `rolescout scout` by hand to confirm it works now. |
| A board has failed more than two runs in a row | The company probably moved job systems. On your own machine run `rolescout companies resolve NAME`, then export and import again; remove the dead board on the Mac with `rolescout companies unmap NAME SOURCE REF`. |
| `Not starting: another scout is already running` | Normal if a run is in progress. A crashed run cannot leave the lock behind. |
| Nothing in `scout.log` at all | Look at `~/Library/Logs/rolescout/launchd.log`, which holds anything printed before rolescout could open its own log. |
