# RoleScout Scraper

Python scraper that fetches open roles from target companies via ATS APIs
(Greenhouse, Lever, Ashby) and scores them against a candidate profile.

Part of the RoleScout pipeline. Hosted on Railway as a FastAPI service.
Web app: https://getrolescout.com

---

## Setup

pip install -r requirements.txt

---

## Usage

# Default — profile mode, fresh run
python scraper.py

# Explicit profile path
python scraper.py --profile path/to/candidate_profile.json

# Force CSV mode (demo/testing)
python scraper.py --csv

# Preserve existing output and merge new results
python scraper.py --preserve

# Verbose output
python scraper.py --verbose

---

## Input

Primary input: `candidate_profile.json`
- `target_companies[]` — companies to scrape
- `role_filters[]` — admission filters (title, pattern, seniority, domain, exclude_title)
- `skills[]` — used for match scoring
- `preferences.excluded_companies[]` — companies skipped entirely

CSV fallback (when no profile, or --csv flag):
- `target_company_list.csv` — columns: company_name, website, tier
- `role_filters.csv` — columns: field, value

---

## Output

- `open_roles.csv` — filtered, scored roles (15 columns)
- `last_run_summary.json` — run metadata, stats, per-company breakdown
- `errors.log` — companies that failed to fetch

Default behavior: deletes all three files before each run (fresh start).
Pass `--preserve` to keep existing files and merge new results.

---

## ATS Support

| ATS        | Detection         | Notes                        |
|------------|-------------------|------------------------------|
| Greenhouse | boards.greenhouse.io | Single list call with content |
| Lever      | jobs.lever.co     | Returns descriptions inline  |
| Ashby      | jobs.ashbyhq.com  | GraphQL, detail fetch post-filter |

Google and Meta are unsupported (JS-rendered job boards).

---

## Match Scoring

- Title match: +35 (guaranteed by admission filter)
- Seniority match: +25
- Domain match: +5 per match, max 25
- Skill match: +1 per match, max 15
- Blank description → blank score (not 0)

---

## Railway FastAPI Service

Endpoint: POST /run-scraper
Accepts: { "profile": candidate_profile }
Returns: SSE stream of progress events + done event with CSV payload

Health check: GET /health

---

## Notes

- Do not modify input files without asking the user
- See job-scraper-requirements.md for the full spec
- Personal runs: ~/personal/rolescout/ (not committed to GitHub)