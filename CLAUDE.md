# Job Scraper

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python scraper.py --verbose
```

## Input files

- `target_company_list.csv` — CSV with `company_name,website,tier` columns
- `role_filters.csv` — Optional CSV with `field,value` columns for filtering roles (e.g. `title,Product Manager`). If missing or empty, no filtering is applied.

## Output

- `open_roles.csv` — Consolidated job listings. Incrementally updated on each run (existing roles are updated, stale roles marked as no longer accepting).
- `last_run_summary.json` — Stats from the most recent run (company/role counts, filters applied, per-ATS breakdown, field population rates).
- `errors.log` — Companies that failed to fetch.

## Notes

- Do not modify `target_company_list.csv` or `role_filters.csv` without asking the user.
- See `job-scraper-requirements.md` for the full spec.
