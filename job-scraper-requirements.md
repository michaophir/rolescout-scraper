# Job Scraper — Requirements for Claude Code

## Overview

A command-line Python application that reads a list of target companies and role filters, fetches open job listings, filters by relevance, scores each role against a candidate profile, and outputs a consolidated CSV with a run summary JSON. Designed to run once on demand with no persistent state.

> **Pipeline context:** This scraper is Step 2 in the RoleScout pipeline. Step 1 (Candidate Profile) produces `candidate_profile.json` as its primary output, with `target_company_list.csv` and `role_filters.csv` as compatibility exports. The scraper prefers `candidate_profile.json` when present; otherwise it falls back to the two CSVs. The `--csv` flag forces CSV mode even when a profile exists. Each input can also be provided manually — the scraper does not require Step 1 to have been run.

---

## Definition of Done

On a single execution, the app:
1. Loads `candidate_profile.json` if present (companies + filters + skills + excluded_companies); otherwise reads `target_company_list.csv` and `role_filters.csv`. The `--csv` flag forces CSV mode.
2. Skips any company listed in `preferences.excluded_companies` from the profile
3. Fetches open roles for each company via ATS API, slug-guessing fallback, or careers page scrape
4. Filters roles using `title` and `pattern` filter rows; drops any role whose title contains an `exclude_title` value
5. Scores each passing role using `seniority`, `domain`, and `skill` (or `profile.skills`) signals
6. Writes results to `open_roles.csv` (overwrites existing by default; `--preserve` switches to incremental merge)
7. Writes a run summary to `last_run_summary.json`
8. Exits cleanly with a summary printed to stdout

**Default is fresh runs.** Before fetching, the scraper deletes `open_roles.csv`, `last_run_summary.json`, and `errors.log` so each run produces clean files. Pass `--preserve` to keep them and merge new results into the existing CSV.

---

## Input Files

### `candidate_profile.json` (preferred input)

JSON produced by Step 1 of the RoleScout pipeline. When this file is present and contains both `target_companies` and `role_filters`, the scraper uses it instead of the CSV files. Pass `--csv` to force CSV mode.

```json
{
  "target_companies": [
    {"company_name": "Anthropic", "website": "https://anthropic.com", "tier": 1}
  ],
  "role_filters": [
    {"field": "title", "value": "Product Manager"},
    {"field": "pattern", "value": "(?i)(product manager|head of product)"},
    {"field": "seniority", "value": "Senior"},
    {"field": "domain", "value": "AI"},
    {"field": "exclude_title", "value": "data"}
  ],
  "skills": ["roadmapping", "B2B", "API"],
  "preferences": {
    "excluded_companies": ["Meta", "Google"]
  }
}
```

- `target_companies[]` — same shape as `target_company_list.csv` rows
- `role_filters[]` — same shape as `role_filters.csv` rows; same field types apply (see filter table below)
- `skills[]` — list of skill strings. When present, **overrides** the `skill` filter rows for match scoring (see Match Scoring)
- `preferences.excluded_companies[]` — case-insensitive company names to skip entirely (no fetch attempt, no `errors.log` entry)
- Configurable via `--profile` CLI argument (default: `candidate_profile.json`)

---

### `target_company_list.csv`

CSV with three required fields:

```
company_name,website,tier
Anthropic,https://anthropic.com,1
Stripe,https://stripe.com,1
Notion,https://notion.so,2
Figma,https://figma.com,2
```

- First row must be the header
- `website` — company homepage, used as base URL for ATS detection
- `tier` — integer priority (1 = highest). Passed through to output CSV as-is
- Blank lines and lines starting with `#` are ignored
- Configurable via `--input` CLI argument (default: `target_company_list.csv`)

---

### `role_filters.csv`

CSV with two fields: `field` and `value`. Controls both admission filtering and match scoring.

```
field,value
title,Chief Product Officer
title,VP of Product
title,Director of Product
title,Head of Product
title,Staff Product Manager
title,Principal Product Manager
title,Senior Product Manager
title,Product Manager
title,Product Lead
title,Founding Product
title,Group Product Manager
title,Product Operations
title,Technical Product Manager
pattern,(?i)(product manager|product lead|head of product|director of product|vp of product|chief product|founding product)
seniority,Principal
seniority,Director
seniority,VP
seniority,Head
seniority,Staff
seniority,Senior
seniority,Founding
domain,fintech
domain,audio
domain,media
domain,adtech
domain,advertising
domain,AI
domain,machine learning
domain,data
skill,roadmapping
skill,data products
skill,cross-functional
skill,API
skill,platform
skill,B2B
skill,enterprise
skill,growth
skill,0 to 1
skill,SQL
skill,Figma
```

#### Field type behavior

| Field type | Admission filter | Scoring | Match target |
|---|---|---|---|
| `title` | ✅ Yes — role must match to pass | ✅ Yes | `job_title` (substring, case-insensitive) |
| `pattern` | ✅ Yes — role must match to pass | ✅ Yes | `job_title` (regex match) |
| `exclude_title` | ✅ Yes — role is **dropped** if matched | ❌ No | `job_title` (substring, case-insensitive) |
| `seniority` | ❌ No | ✅ Yes | `job_title` (substring, case-insensitive) |
| `domain` | ❌ No | ✅ Yes | `description` (substring, case-insensitive) |
| `skill` | ❌ No | ✅ Yes | `description` (substring, case-insensitive) |

**Admission logic:**
1. A role passes admission if its `job_title` matches at least one `title` or `pattern` row.
2. After admission, any role whose `job_title` contains an `exclude_title` value is dropped. `exclude_title` only excludes — it never admits a role on its own.
3. `seniority`, `domain`, and `skill` rows are never used for admission.

**Matching:** All string matching is case-insensitive substring. Python `in` operator on lowercased strings. Pattern rows use `re.search()`.

- The CSV path is hardcoded to `role_filters.csv` (no CLI override). Use a candidate profile if you need a different filter set.

---

## Output Files

### `open_roles.csv`

One row per filtered, scored job listing.

#### Columns (15 fields, in order)

| Column | Description |
|---|---|
| `job_id` | ATS-native ID, or stable hash of `company + job_url` for scraped roles |
| `company` | Company name from `target_company_list.csv` |
| `job_title` | Role title |
| `location` | Office location string or "Remote" |
| `remote` | Boolean (`true` / `false`) |
| `workplace_type` | One of: `remote`, `hybrid`, `onsite`. From Lever/Ashby `workplaceType`. For Greenhouse, derived from `remote` boolean. Blank if unavailable. |
| `department` | Team or department (if available) |
| `date_posted` | `YYYY-MM-DD` format. Blank if unavailable. |
| `accepting_applications` | Boolean (`true` / `false`) |
| `job_url` | Direct URL to the job posting |
| `last_seen` | Date this row was last fetched, `YYYY-MM-DD` |
| `description` | Plain text JD, HTML stripped, truncated to 6000 characters. Blank if unavailable. |
| `compensation_raw` | Raw compensation text, not parsed. Blank if not found. |
| `tier` | Company priority tier from input file |
| `match_score` | Integer 0–100 match score. Blank (not 0) if description is empty and scoring cannot run. |

- UTF-8 encoded
- Missing fields left blank, never omitted
- One row per job listing

#### Write behavior

- **Default (fresh):** `open_roles.csv` is deleted at the start of the run, then written from scratch with only the roles fetched this run.
- **With `--preserve`:** the existing CSV is loaded and treated as a persistent record updated incrementally:
  - **Duplicate detection:** based on `job_id`
  - **Existing role re-fetched:** update all fields, refresh `last_seen`
  - **New role:** append new row
  - **Role no longer returned:** leave row, set `accepting_applications` to `false`

The same delete-vs-preserve rule applies to `last_run_summary.json` and `errors.log`.

---

### `last_run_summary.json`

Written on every run. Contains:

```json
{
  "run_date": "2026-05-01T11:23:47",
  "companies_total": 63,
  "companies_succeeded": 57,
  "companies_failed": ["Glean", "Retool", "Rippling"],
  "filters_applied": [
    "title:product manager",
    "pattern:(?i)(product manager|head of product)",
    "seniority:senior",
    "domain:ai",
    "skill:roadmapping"
  ],
  "filter_coverage": [
    {"type": "title", "value": "product manager", "matches": 81},
    {"type": "pattern", "value": "(?i)(product manager|head of product)", "matches": 12}
  ],
  "roles_fetched_post_filter": 278,
  "field_population": {
    "description": 270,
    "workplace_type": 105,
    "compensation_raw": 91,
    "date_posted": 278,
    "department": 240,
    "tier": 278
  },
  "per_ats": {
    "greenhouse": 180,
    "ashby": 50,
    "lever": 40,
    "careers_page": 8,
    "unknown": 0
  },
  "match_score_stats": {
    "scored": 270,
    "unscored": 8,
    "avg_score": 55,
    "high_matches": 35
  },
  "per_company": [
    {
      "company": "Anthropic",
      "tier": "1",
      "ats": "greenhouse",
      "roles_total": 439,
      "roles_post_filter": 9
    },
    {
      "company": "Glean",
      "tier": "2",
      "ats": "",
      "roles_total": 0,
      "roles_post_filter": 0,
      "error": "No roles found via ATS or careers page"
    }
  ]
}
```

**Important scoping rules:**
- `run_date` — ISO 8601 timestamp (`YYYY-MM-DDTHH:MM:SS`), captured at write time
- `filters_applied` — includes all filter rows **except** `exclude_title` (those are quiet drops, not advertised admission filters)
- `filter_coverage` — includes only `title` and `pattern` rows. Counts admissions per filter (which row admitted each role). Excludes `seniority`, `domain`, `skill`, `exclude_title`
- `per_ats` — keyed by the in-memory `_source` tag. Possible keys: `greenhouse`, `lever`, `ashby`, `careers_page`, `unknown`
- `per_company` — one entry per company processed; failures include an `error` string. Companies skipped via `excluded_companies` are **not** included
- `high_matches` — count of roles with `match_score >= 70`
- `match_score_stats.unscored` — count of roles where description was blank and score was left empty

---

## Match Scoring

Scoring runs after admission filtering. Each role receives a `match_score` from 0–100.

### Formula

| Signal | Source | Points |
|---|---|---|
| Title match | `job_title` contains any `title` value OR matches any `pattern` (guaranteed by admission) | +35 |
| Seniority match | `job_title` contains any `seniority` value (case-insensitive substring) | +25 |
| Domain match | `description` contains domain value (case-insensitive substring) | +5 per match, max 25 |
| Skill match | `description` contains skill value (case-insensitive substring) | +1 per match, max 15 |

**Total: 0–100**

### Skill source: profile vs filters

Skill scoring draws from one of two sources:

- **Profile mode active** (`candidate_profile.json` loaded with both `target_companies` and `role_filters`, AND `skills[]` non-empty): scores against `profile.skills[]`. The `skill` filter rows are ignored.
- **CSV fallback** (or profile lacks `skills[]`): scores against the `skill` rows in `role_filters.csv`.

Both sources cap at +15. Switching sources changes which terms count, not the weight.

### Null score rule

If `description` is blank, set `match_score` to `""` (empty string), not `0`. A blank score means unscored, not a bad match. Roles with blank scores appear at the bottom of the Best Match section in the Review UI, not excluded.

### Title match note

Because admission filtering already guarantees a `title` or `pattern` match, all scored roles receive at least +35. The effective scored range in practice is 35–100.

---

## Fetching Strategy

Three-step cascade per company. The first step that returns roles wins.

### 1. ATS detection from website

Fetch the company `website`, scan the first 50KB of HTML for known ATS embed patterns, and route to the matching fetcher:

| ATS | Detection regex | Fetch endpoint |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/{slug}` or `board.greenhouse.io/{slug}` | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Lever | `jobs.lever.co/{slug}` | `https://api.lever.co/v0/postings/{slug}?mode=json` |
| Ashby | `jobs.ashbyhq.com/{slug}` | `https://jobs.ashbyhq.com/api/non-user-graphql` (GraphQL) |

### 2. Slug-guessing fallback

If detection fails, try slug variants derived from the company name (`acme-co`, `acmeco`, `acme_co`) against every ATS in turn. First non-empty response wins. Marked as `slug guess` in verbose output.

### 3. Careers page scraping

If no ATS works, scan `/careers`, `/jobs`, and `/open-positions` for links containing job-shaped keywords (`/job`, `/position`, `/role`, `/opening`, or known ATS hostnames). Each link becomes a row with `title` only — description, location, comp, and workplace_type are blank. Marked as `careers_page` in per_company output.

### 4. Log and continue

If all three steps fail, log to `errors.log` and add the company to `companies_failed`.

### Per-ATS field details

- **Greenhouse:** Single list call with `?content=true` returns the per-job description blob, avoiding N+1 detail fetches. Description from `content` (HTML — strip tags). Compensation extracted from `.pay-range` or `.content-pay-transparency` blocks within the content HTML. No explicit `workplaceType` field — derived as `remote` if location contains "remote", otherwise blank.
- **Lever:** Single list call returns `descriptionPlain`, `additionalPlain`, and `workplaceType` per posting. Description from `descriptionPlain` (preferred) or `description`. Compensation from `additionalPlain`. Workplace type from `workplaceType`.
- **Ashby:** Two-stage GraphQL fetch against the (non-public) `non-user-graphql` endpoint used by the hosted job board:
  1. **List query** (`ApiJobBoardWithTeams`) returns `id`, `title`, `locationName`, `compensationTierSummary` for all postings — enough to filter on
  2. **Detail query** (`ApiJobPosting`) is **deferred until after admission filtering** to avoid rate-limiting on large boards. It populates `descriptionHtml`, `workplaceType`, `departmentName`, and `publishedDate` for surviving rows only.

  This means scoring runs *after* enrichment, since Ashby descriptions don't exist before the detail fetch.

---

## CLI Interface

```bash
python3 scraper.py [OPTIONS]

Options:
  --input    PATH   Path to target companies CSV (default: target_company_list.csv)
  --output   PATH   Path to output CSV file (default: open_roles.csv)
  --profile  PATH   Path to candidate profile JSON (default: candidate_profile.json)
  --csv             Force CSV input mode; ignore candidate_profile.json
  --preserve        Keep existing open_roles.csv and merge new results (default: overwrite)
  --delay    FLOAT  Seconds to wait between requests (default: 1.0)
  --verbose         Print progress to stdout
```

The role filters CSV path (`role_filters.csv`) and run summary path (`last_run_summary.json`) are hardcoded — not exposed via CLI.

---

## Error Handling

- Must not crash if a single company fetch fails
- Failed companies logged to `errors.log` with timestamp, company name, and reason (`<timestamp> | <company> | <reason>`)
- Companies skipped via `excluded_companies` are **not** logged as failures
- Summary printed at completion (failure tail only included when there are failures):
  ```
  Done! 278 roles found across 57/63 companies. Results written to open_roles.csv | 6 failed (see errors.log)
  Run summary written to last_run_summary.json
  ```

---

## Technical Requirements

- **Language:** Python 3.10+
- **Dependencies:** `requests`, `beautifulsoup4`, `csv` (stdlib), `re` (stdlib)
- Use `beautifulsoup4` or stdlib `html` to strip HTML from descriptions
- `lxml` or `playwright` only if needed for JS-rendered pages
- No database or persistent state
- `requirements.txt` must be included

---

## File Structure

```
rolescout-scraper/
├── scraper.py                  # Main script
├── candidate_profile.json      # Input: preferred — companies + filters + skills + preferences
├── target_company_list.csv     # Input: companies and tiers (CSV fallback)
├── role_filters.csv            # Input: admission filters + scoring config (CSV fallback)
├── open_roles.csv              # Output: filtered, scored roles (incrementally updated)
├── last_run_summary.json       # Output: run metadata and stats
├── errors.log                  # Output: per-company errors
├── requirements.txt            # Python dependencies
├── job-scraper-requirements.md # This document
├── CLAUDE.md                   # Claude Code context file
└── ats_samples/                # Sample ATS API responses for testing
    ├── greenhouse_anthropic.json
    ├── lever_spotify.json
    └── ashby_notion.json
```

---

## Known Limitations

- Description truncated to 6000 characters — skill/domain mentions past that cutoff won't score
- `workplace_type` has low population — many ATS responses don't include it. Greenhouse never sets `hybrid` or `onsite` (only `remote`, derived from location)
- Match scoring uses keyword matching only — no semantic or ML-based matching
- Title scoring is unweighted — all matching title filters score equally (+35). Higher seniority titles do not score higher than generic PM titles in V0
- Ashby description enrichment runs *after* admission filtering, so descriptions for filtered-out Ashby roles remain blank in `open_roles.csv` if those rows were merged from a previous run with different filters
- `careers_page` rows have only `title` and `job_url` populated — descriptions, locations, comp, and workplace_type are blank, so they're effectively unscored

---

## Out of Scope (V0)

- Authentication or login-gated job pages
- Scheduling or recurring runs (single-shot CLI only)
- JS-rendered career pages (no Playwright integration yet — handled by failing fast)
- Semantic or ML-based matching
- Weighted title scoring by seniority level
- Hosted scraper trigger (planned for V1.5 with FastAPI on Railway)
- DuckDB in-memory data layer (planned for V1.5)
