#!/usr/bin/env python3
"""
Company Jobs Consolidator

Fetches and consolidates open positions from multiple companies by querying
common ATS (Applicant Tracking System) public job board APIs:
  - Greenhouse (boards-api.greenhouse.io)
  - Lever (api.lever.co)
  - Ashby (api.ashbyhq.com)

Usage:
  python jobs.py stripe notion linear
  python jobs.py --file companies.txt
  python jobs.py --export-csv results.csv stripe notion
  python jobs.py --export-json results.json stripe notion
"""

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from typing import Optional

import requests


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    department: Optional[str] = None
    employment_type: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Convert a company name into a likely ATS board slug."""
    slug = name.lower().strip()
    for ch in (" ", ".", ",", "'", "&", "/", "\\"):
        slug = slug.replace(ch, "-")
    # collapse multiple dashes
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def slug_variants(name: str) -> list[str]:
    """Return a list of slug variants to try for a given company name."""
    base = slugify(name)
    no_dashes = base.replace("-", "")
    underscored = base.replace("-", "_")
    return list(dict.fromkeys([base, no_dashes, underscored]))  # deduplicated


# ---------------------------------------------------------------------------
# ATS fetchers
# ---------------------------------------------------------------------------

def try_greenhouse(company_name: str) -> Optional[list[Job]]:
    """Try the public Greenhouse job board API (no auth required)."""
    for slug in slug_variants(company_name):
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                jobs: list[Job] = []
                for item in data.get("jobs", []):
                    location = (item.get("location") or {}).get("name", "")
                    dept = None
                    departments = item.get("departments") or []
                    if departments:
                        dept = departments[0].get("name")
                    jobs.append(Job(
                        title=item.get("title", ""),
                        company=company_name,
                        location=location,
                        url=item.get("absolute_url", ""),
                        department=dept,
                    ))
                return jobs
        except (requests.RequestException, ValueError):
            continue
    return None


def try_lever(company_name: str) -> Optional[list[Job]]:
    """Try the public Lever postings API (no auth required)."""
    for slug in slug_variants(company_name):
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    jobs: list[Job] = []
                    for item in data:
                        cats = item.get("categories") or {}
                        jobs.append(Job(
                            title=item.get("text", ""),
                            company=company_name,
                            location=cats.get("location", ""),
                            url=item.get("hostedUrl", ""),
                            department=cats.get("department"),
                            employment_type=cats.get("commitment"),
                        ))
                    return jobs
        except (requests.RequestException, ValueError):
            continue
    return None


def try_ashby(company_name: str) -> Optional[list[Job]]:
    """Try the public Ashby job board API (no auth required)."""
    for slug in slug_variants(company_name):
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                jobs: list[Job] = []
                for item in data.get("jobs", []):
                    jobs.append(Job(
                        title=item.get("title", ""),
                        company=company_name,
                        location=item.get("location", ""),
                        url=item.get("jobUrl", ""),
                        department=item.get("department"),
                        employment_type=item.get("employmentType"),
                    ))
                return jobs
        except (requests.RequestException, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

FETCHERS = [
    (try_greenhouse, "Greenhouse"),
    (try_lever, "Lever"),
    (try_ashby, "Ashby"),
]


def fetch_jobs(company_name: str) -> tuple[list[Job], str]:
    """Try all supported ATS providers and return (jobs, source_name)."""
    for fetcher, source in FETCHERS:
        jobs = fetcher(company_name)
        if jobs is not None:
            return jobs, source
    return [], "Not found"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_results(all_jobs: dict[str, tuple[list[Job], str]]) -> None:
    """Pretty-print consolidated job listings to stdout."""
    total = sum(len(jobs) for jobs, _ in all_jobs.values())

    print()
    print("=" * 72)
    print(f"  CONSOLIDATED JOB LISTINGS  |  {total} open position(s) across "
          f"{len(all_jobs)} company/companies")
    print("=" * 72)

    for company, (jobs, source) in all_jobs.items():
        print()
        badge = f"via {source}" if source != "Not found" else source
        print(f"  {company}  ({len(jobs)} positions, {badge})")
        print(f"  {'-' * 60}")

        if not jobs:
            print("  No open positions found (or company not on a supported ATS).")
            continue

        for job in jobs:
            dept_prefix = f"[{job.department}] " if job.department else ""
            type_suffix = f"  |  {job.employment_type}" if job.employment_type else ""
            loc = job.location or "Location not specified"

            print(f"  • {dept_prefix}{job.title}")
            print(f"      {loc}{type_suffix}")
            if job.url:
                print(f"      {job.url}")

    print()


def export_csv(all_jobs: dict[str, tuple[list[Job], str]], path: str) -> None:
    fields = ["company", "title", "department", "location",
              "employment_type", "url", "source"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for company, (jobs, source) in all_jobs.items():
            for job in jobs:
                writer.writerow({
                    "company": job.company,
                    "title": job.title,
                    "department": job.department or "",
                    "location": job.location,
                    "employment_type": job.employment_type or "",
                    "url": job.url,
                    "source": source,
                })
    print(f"CSV exported to: {path}")


def export_json(all_jobs: dict[str, tuple[list[Job], str]], path: str) -> None:
    rows = []
    for company, (jobs, source) in all_jobs.items():
        for job in jobs:
            rows.append({
                "company": job.company,
                "title": job.title,
                "department": job.department,
                "location": job.location,
                "employment_type": job.employment_type,
                "url": job.url,
                "source": source,
            })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    print(f"JSON exported to: {path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch and consolidate open positions from multiple companies.\n"
            "Queries Greenhouse, Lever, and Ashby public job board APIs.\n\n"
            "Examples:\n"
            "  python jobs.py stripe notion linear\n"
            "  python jobs.py --file companies.txt --export-csv out.csv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "companies",
        nargs="*",
        metavar="COMPANY",
        help="Company name(s) to search (e.g. stripe notion linear)",
    )
    parser.add_argument(
        "--file", "-f",
        metavar="PATH",
        help="Text file with one company name per line",
    )
    parser.add_argument(
        "--export-csv",
        metavar="PATH",
        help="Export consolidated results to a CSV file",
    )
    parser.add_argument(
        "--export-json",
        metavar="PATH",
        help="Export consolidated results to a JSON file",
    )

    args = parser.parse_args()

    # Collect company names from all sources
    companies: list[str] = list(args.companies)

    if args.file:
        try:
            with open(args.file, encoding="utf-8") as fh:
                file_companies = [line.strip() for line in fh if line.strip()]
            companies.extend(file_companies)
        except FileNotFoundError:
            print(f"Error: file not found: {args.file}", file=sys.stderr)
            sys.exit(1)

    # Interactive fallback when no companies provided
    if not companies:
        print("Enter company names (one per line, blank line when done):")
        while True:
            line = input("  > ").strip()
            if not line:
                break
            companies.append(line)

    if not companies:
        print("No companies provided. Exiting.")
        sys.exit(1)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_companies: list[str] = []
    for c in companies:
        if c.lower() not in seen:
            seen.add(c.lower())
            unique_companies.append(c)

    print(f"\nFetching open positions for {len(unique_companies)} company/companies...\n")

    all_jobs: dict[str, tuple[list[Job], str]] = {}
    for company in unique_companies:
        print(f"  Searching: {company} ...", end=" ", flush=True)
        jobs, source = fetch_jobs(company)
        all_jobs[company] = (jobs, source)
        count_str = f"{len(jobs)} position(s)" if source != "Not found" else "not found on Greenhouse / Lever / Ashby"
        print(count_str)

    print_results(all_jobs)

    if args.export_csv:
        export_csv(all_jobs, args.export_csv)

    if args.export_json:
        export_json(all_jobs, args.export_json)


if __name__ == "__main__":
    main()
