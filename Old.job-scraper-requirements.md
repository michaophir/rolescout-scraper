# Job Scraper — Requirements for Claude Code

## Overview

A command-line Python application that reads a list of companies from a file, fetches their open job listings, and outputs a consolidated CSV. Designed to run once on demand with no persistent state.

> **Future context:** This app is the second step in a planned two-step pipeline. A separate upstream tool (not in scope here) will take a candidate's resume and preferences (location, role type, etc.) as input and generate the `companies.txt` file automatically. This scraper should be designed to accept that file as-is without modification.

---

## Definition of Done

On a single execution, the app:
1. Reads a list of company names from an input file
2. Discovers and fetches open roles for each company
3. Writes all results to a single CSV file
4. Exits cleanly with a summary printed to stdout

---

## Input

**File:** `companies.txt` — CSV format with two fields: `company_name` and `website`

```
company_name,website
Anthropic,https://anthropic.com
Stripe,https://stripe.com
Notion,https://notion.so
Figma,https://figma.com
```

- First row must be the header (`company_name,website`)
- `website` is the company's main homepage — the app should use it as the base URL when detecting ATS or locating the careers page, rather than guessing from the company name
- Blank lines and lines starting with `#` should be ignored
- The file path should be configurable via a CLI argument (default: `companies.txt`)

---

## Output

**File:** `open_roles.csv` (default name, configurable via CLI argument)

### Required Columns

| Column | Description |
|---|---|
| `job_id` | Unique identifier from the ATS or a stable hash of `company + job_url` for scraped roles |
| `company` | Company name as provided in input |
| `job_title` | Title of the open role |
| `location` | Office location or "Remote" |
| `remote` | Boolean (`true` / `false`) |
| `department` | Team or department (if available) |
| `date_posted` | Date posted in `YYYY-MM-DD` format (if available) |
| `accepting_applications` | Boolean (`true` / `false`) |
| `job_url` | Direct URL to the job posting |
| `last_seen` | Date this row was last fetched/updated, in `YYYY-MM-DD` format |

- Missing or unavailable fields should be left blank (not omitted)
- Output should be UTF-8 encoded
- One row per job listing

### Merge Behavior (Incremental Updates)

The output CSV is treated as a persistent record that is updated on each run, not overwritten wholesale.

- **Duplicate detection** is based on `job_id` (ATS-native ID where available; otherwise a stable hash of `company + job_url`)
- **Existing role re-fetched:** update all fields in the row with the latest data and refresh `last_seen`
- **New role found:** append a new row
- **Role no longer returned by the source:** leave the row in the CSV as-is (do not delete); set `accepting_applications` to `false` if it was previously `true`

---

## Fetching Strategy

Claude Code should determine the best approach per company. Recommended priority order:

1. **Known ATS APIs** — If the company uses a recognized Applicant Tracking System (ATS), use its public API or JSON feed directly:
   - **Greenhouse:** `https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs`
   - **Lever:** `https://api.lever.co/v0/postings/{company_slug}?mode=json`
   - **Workday, Ashby, Rippling, etc.:** use known JSON endpoints where available
2. **Careers page scraping** — If no ATS is detected, attempt to find and scrape the company's `/careers` or `/jobs` page
3. **Fallback** — If neither works, log the company as `failed` and continue

Claude Code should implement a detection layer that attempts to identify which ATS a company uses before fetching.

---

## CLI Interface

```bash
python scraper.py [OPTIONS]

Options:
  --input   PATH   Path to companies input file (default: companies.txt)
  --output  PATH   Path to output CSV file (default: open_roles.csv)
  --delay   FLOAT  Seconds to wait between requests (default: 1.0)
  --verbose        Print progress to stdout
```

---

## Error Handling

- The app must not crash if a single company fetch fails
- Failed companies should be logged to `errors.log` with timestamp, company name, and reason
- At completion, print a summary:
  ```
  Done. 142 roles found across 8/10 companies. 2 failed (see errors.log).
  ```

---

## Technical Requirements

- **Language:** Python 3.10+
- **Dependencies:** `requests`, `beautifulsoup4`, `csv` (stdlib)
- Add `lxml` or `playwright` only if needed for JS-rendered pages
- No database or persistent state required
- A `requirements.txt` must be included

---

## Out of Scope

- No authentication or login-gated job pages
- No scheduling or recurring runs
- No filtering by role type, seniority, or keyword (fetch all open roles)
