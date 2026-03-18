#!/usr/bin/env python3
"""
Job Scraper — Fetches open roles from companies via ATS APIs and careers pages.

Usage:
    python scraper.py
    python scraper.py --input companies.txt --output open_roles.csv --verbose
"""

import argparse
import csv
import hashlib
import io
import logging
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

TODAY = date.today().isoformat()

OUTPUT_FIELDS = [
    "job_id", "company", "job_title", "location", "remote",
    "department", "date_posted", "accepting_applications", "job_url", "last_seen",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def stable_job_id(company: str, job_url: str) -> str:
    """Generate a stable hash ID from company + job_url."""
    return hashlib.sha256(f"{company}|{job_url}".encode()).hexdigest()[:16]


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")
    return re.sub(r"-+", "-", slug)


def slug_variants(name: str) -> list[str]:
    base = slugify(name)
    return list(dict.fromkeys([base, base.replace("-", ""), base.replace("-", "_")]))


def is_remote(location: str) -> str:
    """Return 'true'/'false' based on location string."""
    return "true" if location and "remote" in location.lower() else "false"


def make_row(company: str, title: str, location: str, url: str,
             department: str = "", date_posted: str = "", job_id: str = "") -> dict:
    return {
        "job_id": job_id or stable_job_id(company, url),
        "company": company,
        "job_title": title,
        "location": location or "",
        "remote": is_remote(location),
        "department": department,
        "date_posted": date_posted,
        "accepting_applications": "true",
        "job_url": url,
        "last_seen": TODAY,
    }


# ---------------------------------------------------------------------------
# Error logger
# ---------------------------------------------------------------------------

def setup_error_log() -> logging.Logger:
    logger = logging.getLogger("scraper_errors")
    logger.setLevel(logging.ERROR)
    handler = logging.FileHandler("errors.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
    logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------
# ATS detection
# ---------------------------------------------------------------------------

def detect_ats(website: str, session: requests.Session) -> tuple[str, str]:
    """Try to detect ATS from the company website. Returns (ats_name, slug)."""
    try:
        resp = session.get(website, timeout=10, allow_redirects=True)
        body = resp.text[:50_000]
    except requests.RequestException:
        return "", ""

    # Check for Greenhouse
    m = re.search(r'boards\.greenhouse\.io/(\w+)', body)
    if m:
        return "greenhouse", m.group(1)
    m = re.search(r'board\.greenhouse\.io/(\w+)', body)
    if m:
        return "greenhouse", m.group(1)

    # Check for Lever
    m = re.search(r'jobs\.lever\.co/(\w[\w-]*)', body)
    if m:
        return "lever", m.group(1)

    # Check for Ashby
    m = re.search(r'jobs\.ashbyhq\.com/([\w-]+)', body)
    if m:
        return "ashby", m.group(1)

    return "", ""


# ---------------------------------------------------------------------------
# ATS fetchers
# ---------------------------------------------------------------------------

def fetch_greenhouse(slug: str, company: str, session: requests.Session) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = session.get(url, timeout=10)
    if resp.status_code != 200:
        return []
    rows = []
    for item in resp.json().get("jobs", []):
        loc = (item.get("location") or {}).get("name", "")
        depts = item.get("departments") or []
        dept = depts[0].get("name", "") if depts else ""
        posted = (item.get("updated_at") or "")[:10]
        rows.append(make_row(
            company=company, title=item.get("title", ""), location=loc,
            url=item.get("absolute_url", ""), department=dept,
            date_posted=posted, job_id=str(item.get("id", "")),
        ))
    return rows


def fetch_lever(slug: str, company: str, session: requests.Session) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = session.get(url, timeout=10)
    if resp.status_code != 200:
        return []
    data = resp.json()
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        cats = item.get("categories") or {}
        posted = ""
        ts = item.get("createdAt")
        if ts:
            posted = date.fromtimestamp(ts / 1000).isoformat()
        rows.append(make_row(
            company=company, title=item.get("text", ""),
            location=cats.get("location", ""),
            url=item.get("hostedUrl", ""),
            department=cats.get("department", ""),
            date_posted=posted, job_id=item.get("id", ""),
        ))
    return rows


def fetch_ashby(slug: str, company: str, session: requests.Session) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    resp = session.get(url, timeout=10)
    if resp.status_code != 200:
        return []
    rows = []
    for item in resp.json().get("jobs", []):
        rows.append(make_row(
            company=company, title=item.get("title", ""),
            location=item.get("location", ""),
            url=item.get("jobUrl", ""),
            department=item.get("department", ""),
            job_id=item.get("id", ""),
        ))
    return rows


ATS_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
}


# ---------------------------------------------------------------------------
# Slug-guessing fallback (try all ATS with name-based slugs)
# ---------------------------------------------------------------------------

def try_all_ats(company: str, session: requests.Session) -> list[dict]:
    for slug in slug_variants(company):
        for ats_name, fetcher in ATS_FETCHERS.items():
            try:
                rows = fetcher(slug, company, session)
                if rows:
                    return rows
            except requests.RequestException:
                continue
    return []


# ---------------------------------------------------------------------------
# Careers page scraping fallback
# ---------------------------------------------------------------------------

def scrape_careers_page(website: str, company: str, session: requests.Session) -> list[dict]:
    """Attempt to find job links on /careers or /jobs pages."""
    base = website.rstrip("/")
    rows = []
    for path in ["/careers", "/jobs", "/open-positions"]:
        try:
            resp = session.get(base + path, timeout=10)
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.find_all("a", href=True):
                href = link["href"]
                text = link.get_text(strip=True)
                if not text or len(text) < 3 or len(text) > 200:
                    continue
                # Heuristic: links containing job-related path segments
                if any(kw in href.lower() for kw in ["/job", "/position", "/role", "/opening", "greenhouse", "lever", "ashby", "workday"]):
                    if not href.startswith("http"):
                        href = base + "/" + href.lstrip("/")
                    rows.append(make_row(company=company, title=text, location="", url=href))
            if rows:
                return rows
        except requests.RequestException:
            continue
    return rows


# ---------------------------------------------------------------------------
# Input / Output
# ---------------------------------------------------------------------------

def read_filters(path: str = "filters.csv") -> list[dict]:
    """Read filters CSV (field,value). Returns empty list if file missing or empty."""
    if not Path(path).exists():
        return []
    filters = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            field = row.get("field", "").strip().lower()
            value = row.get("value", "").strip().lower()
            if field and value:
                filters.append({"field": field, "value": value})
    return filters


def apply_filters(rows: list[dict], filters: list[dict]) -> list[dict]:
    """Keep only rows where at least one filter matches (case-insensitive substring)."""
    if not filters:
        return rows
    filtered = []
    for row in rows:
        for f in filters:
            field_key = f["field"]
            # Map filter field names to output field names
            key = "job_title" if field_key == "title" else field_key
            if key in row and f["value"] in row[key].lower():
                filtered.append(row)
                break
    return filtered


def read_companies(path: str) -> list[dict]:
    """Read companies CSV (company_name,website). Skips blanks and comments."""
    companies = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("company_name", "").strip()
            site = row.get("website", "").strip()
            if name and not name.startswith("#"):
                companies.append({"company_name": name, "website": site})
    return companies


def load_existing(path: str) -> dict[str, dict]:
    """Load existing output CSV into a dict keyed by job_id."""
    existing = {}
    if not Path(path).exists():
        return existing
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            jid = row.get("job_id", "")
            if jid:
                existing[jid] = row
    return existing


def write_output(path: str, rows: dict[str, dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for row in rows.values():
            writer.writerow({k: row.get(k, "") for k in OUTPUT_FIELDS})


def merge_rows(existing: dict[str, dict], new_rows: list[dict], seen_ids: set) -> dict[str, dict]:
    """Merge new rows into existing data. Track seen IDs for stale detection."""
    for row in new_rows:
        jid = row["job_id"]
        seen_ids.add(jid)
        existing[jid] = row  # update or insert
    return existing


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch open job listings from companies.")
    parser.add_argument("--input", default="companies.txt", help="Path to companies CSV (default: companies.txt)")
    parser.add_argument("--output", default="open_roles.csv", help="Path to output CSV (default: open_roles.csv)")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests (default: 1.0)")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stdout")
    args = parser.parse_args()

    error_log = setup_error_log()
    filters = read_filters()
    companies = read_companies(args.input)
    existing = load_existing(args.output)
    seen_ids: set[str] = set()
    session = requests.Session()
    session.headers["User-Agent"] = "JobScraper/1.0"

    total_roles = 0
    succeeded = 0
    failed_companies = []

    for i, entry in enumerate(companies):
        name = entry["company_name"]
        website = entry["website"]

        if args.verbose:
            print(f"[{i+1}/{len(companies)}] {name} ...", end=" ", flush=True)

        try:
            rows = []

            # 1. Try ATS detection from website
            if website:
                ats, slug = detect_ats(website, session)
                if ats and slug:
                    fetcher = ATS_FETCHERS[ats]
                    rows = fetcher(slug, name, session)
                    if args.verbose and rows:
                        print(f"{len(rows)} roles ({ats})")

            # 2. Fallback: guess slugs across all ATS
            if not rows:
                rows = try_all_ats(name, session)
                if args.verbose and rows:
                    print(f"{len(rows)} roles (slug guess)")

            # 3. Fallback: scrape careers page
            if not rows and website:
                rows = scrape_careers_page(website, name, session)
                if args.verbose and rows:
                    print(f"{len(rows)} roles (careers page)")

            if rows:
                rows = apply_filters(rows, filters)
                merge_rows(existing, rows, seen_ids)
                total_roles += len(rows)
                succeeded += 1
            else:
                if args.verbose:
                    print("no roles found")
                failed_companies.append(name)
                error_log.error(f"{name} | No roles found via ATS or careers page")

        except Exception as e:
            if args.verbose:
                print(f"ERROR: {e}")
            failed_companies.append(name)
            error_log.error(f"{name} | {e}")

        if i < len(companies) - 1:
            time.sleep(args.delay)

    # Mark stale roles as no longer accepting
    for jid, row in existing.items():
        if jid not in seen_ids and row.get("accepting_applications") == "true":
            row["accepting_applications"] = "false"

    write_output(args.output, existing)

    write_msg = f"Results written to {args.output}"
    fail_msg = f" | {len(failed_companies)} failed (see errors.log)" if failed_companies else ""
    print(f"\nDone! {total_roles} roles found across {succeeded}/{len(companies)} companies. "
          f"{write_msg}{fail_msg}")


if __name__ == "__main__":
    main()
