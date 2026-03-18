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

- `companies.txt` — CSV with `company_name,website` columns
- `filters.csv` — Optional CSV with `field,value` columns for filtering roles (e.g. `title,Product Manager`). If missing or empty, no filtering is applied.

## Output

- `open_roles.csv` — Consolidated job listings. Incrementally updated on each run (existing roles are updated, stale roles marked as no longer accepting).
- `errors.log` — Companies that failed to fetch.

## Notes

- Do not modify `companies.txt` or `filters.csv` without asking the user.
- See `job-scraper-requirements.md` for the full spec.
