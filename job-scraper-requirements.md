# Job Scraper — Requirements for Claude Code

## Overview

A command-line Python application that reads a list of target companies and role filters, fetches open job listings, filters by relevance, scores each role against a candidate profile, and outputs a consolidated CSV with a run summary JSON. Designed to run once on demand with no persistent state.

> **Pipeline context:** This scraper is Step 2 in the RoleScout pipeline. Step 1 (Candidate Profile) produces `candidate_profile.json`, `target_company_list.csv`, and `role_filters.csv` as outputs. The scraper consumes all three. Each input file can also be provided manually — the scraper does not require Step 1 to have been run.

---

## Definition of Done

On a single execution, the app:
1. Reads target companies from `target_company_list.csv`
2. Reads role filters and scoring config from `role_filters.csv`
3. Fetches open roles for each company via ATS API or careers page
4. Filters roles using `title` and `pattern` filter rows
5. Scores each passing role using `seniority`, `domain`, and `skill` filter rows
6. Writes all filtered, scored results to `open_roles.csv`
7. Writes a run summary to `last_run_summary.json`
8. Exits cleanly with a summary printed to stdout

---

## Input Files

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
- Configurable via `--companies` CLI argument (default: `target_company_list.csv`)

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
| `seniority` | ❌ No | ✅ Yes | `job_title` (substring, case-insensitive) |
| `domain` | ❌ No | ✅ Yes | `description` (substring, case-insensitive) |
| `skill` | ❌ No | ✅ Yes | `description` (substring, case-insensitive) |

**Admission logic:** A role passes filtering if its `job_title` matches at least one `title` or `pattern` row. `seniority`, `domain`, and `skill` rows are never used for admission.

**Matching:** All string matching is case-insensitive substring. Python `in` operator on lowercased strings. Pattern rows use `re.search()`.

- Configurable via `--filters` CLI argument (default: `role_filters.csv`)

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
| `description` | Plain text JD, HTML stripped, truncated to 2000 characters. Blank if unavailable. |
| `compensation_raw` | Raw compensation text, not parsed. Blank if not found. |
| `tier` | Company priority tier from input file |
| `match_score` | Integer 0–100 match score. Blank (not 0) if description is empty and scoring cannot run. |

- UTF-8 encoded
- Missing fields left blank, never omitted
- One row per job listing

#### Merge behavior (incremental updates)

The output CSV is treated as a persistent record updated on each run, not overwritten wholesale.

- **Duplicate detection:** based on `job_id`
- **Existing role re-fetched:** update all fields, refresh `last_seen`
- **New role:** append new row
- **Role no longer returned:** leave row, set `accepting_applications` to `false`

---

### `last_run_summary.json`

Written on every run. Contains:

```json
{
  "run_date": "2026-04-15",
  "companies_total": 22,
  "companies_succeeded": 21,
  "companies_failed": ["Meta"],
  "filters_applied": [
    "title:product manager",
    "title:staff product manager",
    "pattern:(?i)..."
  ],
  "filter_coverage": [
    {"type": "title", "value": "product manager", "matches": 81},
    {"type": "title", "value": "staff product manager", "matches": 31}
  ],
  "roles_fetched_post_filter": 153,
  "field_population": {
    "description": 153,
    "workplace_type": 57,
    "compensation_raw": 91,
    "date_posted": 153,
    "department": 153,
    "tier": 153
  },
  "per_ats": {
    "greenhouse": 100,
    "ashby": 33,
    "lever": 20
  },
  "match_score_stats": {
    "scored": 153,
    "unscored": 0,
    "avg_score": 55,
    "high_matches": 35
  },
  "per_company": [
    {
      "company": "Anthropic",
      "tier": "1",
      "ats": "greenhouse",
      "roles_total": 435,
      "roles_post_filter": 9
    }
  ]
}
```

**Important scoping rules:**
- `filters_applied` — includes only `title` and `pattern` rows. Excludes `seniority`, `domain`, `skill` rows
- `filter_coverage` — includes only `title` and `pattern` rows. Excludes `seniority`, `domain`, `skill` rows
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

### Null score rule

If `description` is blank, set `match_score` to `""` (empty string), not `0`. A blank score means unscored, not a bad match. Roles with blank scores appear at the bottom of the Best Match section in the Review UI, not excluded.

### Title match note

Because admission filtering already guarantees a `title` or `pattern` match, all scored roles receive at least +35. The effective scored range in practice is 35–100.

---

## Fetching Strategy

Priority order per company:

1. **Known ATS APIs** — detect and use public JSON endpoints:
   - **Greenhouse:** `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs`
     - Description from `content` (HTML — strip tags)
     - Compensation from within `content` HTML — extract if present
   - **Lever:** `https://api.lever.co/v0/postings/{slug}?mode=json`
     - Description from `descriptionPlain` (preferred) or `description`
     - Compensation from `additionalPlain`
     - Workplace type from `workplaceType`
   - **Ashby:** known JSON endpoints
     - Description from `descriptionHtml` (strip HTML)
     - Compensation from `compensationTierSummary` (often null)
     - Workplace type from `workplaceType`
2. **Careers page scraping** — fallback to `/careers` or `/jobs` page
3. **Log and continue** — if neither works, log to `errors.log` and proceed

**ATS detection layer** runs before fetching. Attempts to identify which ATS a company uses based on known slugs and URL patterns.

**Known unsupported companies:**
- Google — JS-rendered, custom ATS. Returns ~2 roles via careers page scraping. Marked as `careers_page` in per_company output.
- Meta — JS-rendered, no ATS detected. Returns 0 roles. Marked as failed in per_company output.

---

## CLI Interface

```bash
python scraper.py [OPTIONS]

Options:
  --companies  PATH   Path to target companies file (default: target_company_list.csv)
  --filters    PATH   Path to role filters file (default: role_filters.csv)
  --output     PATH   Path to output CSV file (default: open_roles.csv)
  --summary    PATH   Path to run summary JSON (default: last_run_summary.json)
  --delay      FLOAT  Seconds to wait between requests (default: 1.0)
  --verbose           Print progress to stdout
```

---

## Error Handling

- Must not crash if a single company fetch fails
- Failed companies logged to `errors.log` with timestamp, company name, and reason
- Summary printed at completion:
  ```
  Done. 153 roles found across 21/22 companies. 1 failed (see errors.log).
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
sandbox/
├── scraper.py                  # Main script
├── target_company_list.csv     # Input: companies and tiers
├── role_filters.csv            # Input: admission filters + scoring config
├── open_roles.csv              # Output: filtered, scored roles
├── last_run_summary.json       # Output: run metadata and stats
├── errors.log                  # Output: per-company errors
├── requirements.txt            # Python dependencies
├── CLAUDE.md                   # Claude Code context file
└── ats_samples/                # Sample ATS API responses for testing
    ├── greenhouse_anthropic.json
    ├── lever_spotify.json
    └── ashby_notion.json
```

---

## Known Limitations

- Description truncated to 2000 characters — skill/domain mentions past that cutoff won't score
- `workplace_type` has low population (~37% of roles) — many ATS responses don't include it
- Match scoring uses keyword matching only — no semantic or ML-based matching
- Title scoring is unweighted — all matching title filters score equally (+35). Higher seniority titles do not score higher than generic PM titles in V0

---

## Out of Scope (V0)

- Authentication or login-gated job pages
- Scheduling or recurring runs
- Semantic or ML-based matching
- Weighted title scoring by seniority level
- Company Composer integration (planned for Step 1 of pipeline)
- Hosted scraper trigger (planned for V1.5 with FastAPI on Railway)
- DuckDB in-memory data layer (planned for V1.5)
