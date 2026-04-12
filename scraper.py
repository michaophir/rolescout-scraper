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
import html
import io
import json
import logging
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

TODAY = date.today().isoformat()

DESCRIPTION_MAX_CHARS = 2000
WHITESPACE_RE = re.compile(r"\s+")

OUTPUT_FIELDS = [
    "job_id", "company", "job_title", "location", "remote", "workplace_type",
    "department", "date_posted", "accepting_applications", "job_url", "last_seen",
    "description", "compensation_raw", "tier",
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


def strip_html(raw: str) -> str:
    """Decode HTML entities, strip tags, and collapse whitespace."""
    if not raw:
        return ""
    # Entities may be double-encoded (e.g. Greenhouse `content` uses &lt;p&gt;) —
    # unescape the input before handing it to the parser so tags become real tags.
    decoded = html.unescape(raw)
    text = BeautifulSoup(decoded, "html.parser").get_text(separator=" ")
    text = html.unescape(text)
    return WHITESPACE_RE.sub(" ", text).strip()


def truncate(text: str, limit: int = DESCRIPTION_MAX_CHARS) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit]


def normalize_workplace_type(value) -> str:
    """Normalize ATS workplaceType values to remote / hybrid / onsite. Blank otherwise."""
    if not value:
        return ""
    s = str(value).strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if s in {"remote", "fullyremote"}:
        return "remote"
    if s == "hybrid":
        return "hybrid"
    if s in {"onsite", "inoffice", "inperson"}:
        return "onsite"
    return ""


def extract_greenhouse_compensation(content_html: str) -> str:
    """Pull compensation text from a Greenhouse `content` HTML blob, if present."""
    if not content_html:
        return ""
    decoded = html.unescape(content_html)
    soup = BeautifulSoup(decoded, "html.parser")
    pay_range = soup.select_one(".pay-range")
    if pay_range:
        txt = WHITESPACE_RE.sub(" ", pay_range.get_text(separator=" ")).strip()
        if txt:
            return txt
    pay_block = soup.select_one(".content-pay-transparency")
    if pay_block:
        txt = WHITESPACE_RE.sub(" ", pay_block.get_text(separator=" ")).strip()
        if txt:
            return truncate(txt, 500)
    return ""


def make_row(company: str, title: str, location: str, url: str,
             department: str = "", date_posted: str = "", job_id: str = "",
             workplace_type: str = "", description: str = "",
             compensation_raw: str = "", source: str = "") -> dict:
    return {
        "job_id": job_id or stable_job_id(company, url),
        "company": company,
        "job_title": title,
        "location": location or "",
        "remote": is_remote(location),
        "workplace_type": workplace_type,
        "department": department,
        "date_posted": date_posted,
        "accepting_applications": "true",
        "job_url": url,
        "last_seen": TODAY,
        "description": truncate(description),
        "compensation_raw": compensation_raw or "",
        "tier": "",
        # Internal (not written to CSV): which fetcher produced this row.
        # Consumed by the run-summary writer, then discarded.
        "_source": source,
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
    # Single list call with ?content=true returns the same `content` blob the
    # per-job detail endpoint does, so we avoid N+1 requests.
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = session.get(url, timeout=15)
    if resp.status_code != 200:
        return []
    rows = []
    for item in resp.json().get("jobs", []):
        loc = (item.get("location") or {}).get("name", "")
        depts = item.get("departments") or []
        dept = depts[0].get("name", "") if depts else ""
        posted = (item.get("updated_at") or "")[:10]
        content_html = item.get("content", "") or ""
        description = strip_html(content_html)
        compensation = extract_greenhouse_compensation(content_html)
        # Greenhouse has no explicit workplaceType field. Per the spec, derive
        # from the remote signal on location; hybrid/onsite can't be inferred.
        workplace = "remote" if is_remote(loc) == "true" else ""
        rows.append(make_row(
            company=company, title=item.get("title", ""), location=loc,
            url=item.get("absolute_url", ""), department=dept,
            date_posted=posted, job_id=str(item.get("id", "")),
            workplace_type=workplace, description=description,
            compensation_raw=compensation, source="greenhouse",
        ))
    return rows


def fetch_lever(slug: str, company: str, session: requests.Session) -> list[dict]:
    # Lever's list endpoint already returns descriptionPlain, additionalPlain,
    # and workplaceType per posting — a single call gives us everything.
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = session.get(url, timeout=15)
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
            try:
                posted = date.fromtimestamp(ts / 1000).isoformat()
            except (ValueError, OSError, OverflowError):
                posted = ""
        description = item.get("descriptionPlain") or strip_html(item.get("description", ""))
        compensation = (item.get("additionalPlain") or "").strip()
        workplace = normalize_workplace_type(item.get("workplaceType"))
        rows.append(make_row(
            company=company, title=item.get("text", ""),
            location=cats.get("location", ""),
            url=item.get("hostedUrl", ""),
            department=cats.get("department", ""),
            date_posted=posted, job_id=item.get("id", ""),
            workplace_type=workplace, description=description,
            compensation_raw=compensation, source="lever",
        ))
    return rows


ASHBY_GRAPHQL_URL = "https://jobs.ashbyhq.com/api/non-user-graphql"

ASHBY_LIST_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
    jobPostings {
      id
      title
      teamId
      locationName
      employmentType
      compensationTierSummary
    }
  }
}
"""

ASHBY_DETAIL_QUERY = """
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
    jobPostingId: $jobPostingId
  ) {
    id
    title
    departmentName
    locationName
    workplaceType
    employmentType
    descriptionHtml
    publishedDate
    compensationTierSummary
  }
}
"""


def _ashby_graphql(session: requests.Session, op: str, query: str, variables: dict) -> dict:
    payload = {"operationName": op, "variables": variables, "query": query}
    try:
        resp = session.post(
            f"{ASHBY_GRAPHQL_URL}?op={op}",
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException:
        return {}
    if resp.status_code != 200:
        return {}
    try:
        return resp.json() or {}
    except ValueError:
        return {}


def fetch_ashby(slug: str, company: str, session: requests.Session) -> list[dict]:
    # Ashby's public posting-api list endpoint doesn't return descriptions or
    # workplaceType, so we use the non-user GraphQL endpoint the hosted job
    # board uses. The list query gives us title/location/comp, enough to
    # filter on. Description + workplaceType come from a per-job detail
    # query, which we intentionally DEFER until after row filtering (see
    # enrich_ashby_row) — otherwise boards with hundreds of jobs get
    # rate-limited fetching detail for postings we'll throw away.
    list_data = _ashby_graphql(
        session, "ApiJobBoardWithTeams", ASHBY_LIST_QUERY,
        {"organizationHostedJobsPageName": slug},
    )
    postings = (
        ((list_data.get("data") or {}).get("jobBoard") or {}).get("jobPostings") or []
    )
    if not postings:
        return []

    rows = []
    for posting in postings:
        job_id = posting.get("id", "")
        if not job_id:
            continue
        compensation = (posting.get("compensationTierSummary") or "").strip()
        job_url = f"https://jobs.ashbyhq.com/{slug}/{job_id}"
        row = make_row(
            company=company, title=posting.get("title", ""),
            location=posting.get("locationName", "") or "",
            url=job_url,
            department="",  # departmentName only available on detail
            date_posted="", job_id=job_id,
            workplace_type="", description="",
            compensation_raw=compensation, source="ashby",
        )
        # Marker consumed later to populate description / workplaceType.
        row["_ashby_enrich"] = slug
        rows.append(row)
    return rows


def enrich_ashby_row(row: dict, session: requests.Session) -> None:
    """Fetch descriptionHtml + workplaceType + departmentName for a single Ashby row."""
    slug = row.pop("_ashby_enrich", None)
    if not slug:
        return
    job_id = row.get("job_id", "")
    if not job_id:
        return
    detail_data = _ashby_graphql(
        session, "ApiJobPosting", ASHBY_DETAIL_QUERY,
        {"organizationHostedJobsPageName": slug, "jobPostingId": job_id},
    )
    detail = ((detail_data.get("data") or {}).get("jobPosting")) or {}
    if not detail:
        return
    description = strip_html(detail.get("descriptionHtml", ""))
    if description:
        row["description"] = truncate(description)
    workplace = normalize_workplace_type(detail.get("workplaceType"))
    if workplace:
        row["workplace_type"] = workplace
    dept = detail.get("departmentName") or ""
    if dept and not row.get("department"):
        row["department"] = dept
    posted = (detail.get("publishedDate") or "")[:10]
    if posted and not row.get("date_posted"):
        row["date_posted"] = posted
    # Detail may surface a compensationTierSummary that was missing from the list.
    if not row.get("compensation_raw"):
        comp = (detail.get("compensationTierSummary") or "").strip()
        if comp:
            row["compensation_raw"] = comp


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
                    rows.append(make_row(company=company, title=text, location="", url=href, source="careers_page"))
            if rows:
                return rows
        except requests.RequestException:
            continue
    return rows


# ---------------------------------------------------------------------------
# Input / Output
# ---------------------------------------------------------------------------

def read_filters(path: str = "role_filters.csv") -> list[dict]:
    """Read filters CSV (field,value). Returns empty list if file missing or empty.

    Supported field types:
      - ``title``   — case-insensitive substring match against job_title.
      - ``pattern`` — case-insensitive regex match against job_title (uses ``re.search``).
    """
    if not Path(path).exists():
        return []
    filters = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            field = row.get("field", "").strip().lower()
            value = row.get("value", "").strip()
            if not field or not value:
                continue
            entry: dict = {"field": field, "value": value}
            if field == "pattern":
                try:
                    entry["_compiled"] = re.compile(value, re.IGNORECASE)
                except re.error as e:
                    logging.getLogger("scraper_errors").error(
                        f"Invalid regex in filter: {value!r} — {e}"
                    )
                    continue
            else:
                # Substring filters are case-insensitive — lower the value once.
                entry["value"] = value.lower()
            filters.append(entry)
    return filters


def apply_filters(rows: list[dict], filters: list[dict]) -> list[dict]:
    """Keep only rows where at least one filter matches.

    Each retained row gets a ``_matched_filter`` tag (``field:value``) so the
    run summary can report per-filter coverage.
    """
    if not filters:
        return rows
    filtered = []
    for row in rows:
        title = (row.get("job_title") or "").lower()
        for f in filters:
            matched = False
            if f["field"] == "title":
                matched = f["value"] in title
            elif f["field"] == "pattern":
                compiled = f.get("_compiled")
                if compiled and compiled.search(row.get("job_title") or ""):
                    matched = True
            else:
                # Generic fallback for future field types.
                key = "job_title" if f["field"] == "title" else f["field"]
                if key in row and f["value"] in row[key].lower():
                    matched = True
            if matched:
                row["_matched_filter"] = f"{f['field']}:{f['value']}"
                filtered.append(row)
                break
    return filtered


def read_companies(path: str) -> list[dict]:
    """Read companies CSV (company_name,website,tier). Skips blanks and comments.

    The `tier` column is optional — if absent or blank, tier is left empty.
    """
    companies = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("company_name") or "").strip()
            site = (row.get("website") or "").strip()
            tier = (row.get("tier") or "").strip()
            if name and not name.startswith("#"):
                companies.append({"company_name": name, "website": site, "tier": tier})
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


def write_run_summary(
    path: str,
    companies_total: int,
    companies_succeeded: int,
    failed_companies: list[str],
    filters: list[dict],
    run_rows: list[dict],
    company_stats: list[dict],
) -> None:
    """Write last_run_summary.json with per-run stats.

    `run_rows` is the list of rows fetched this run (post-filter). We use
    the in-memory `_source` tag on each row for the per-ATS breakdown — it's
    not persisted to the CSV.
    """
    total = len(run_rows)

    def populated(field: str) -> int:
        return sum(1 for r in run_rows if (r.get(field) or "").strip())

    per_ats: dict[str, int] = {}
    for r in run_rows:
        key = r.get("_source") or "unknown"
        per_ats[key] = per_ats.get(key, 0) + 1

    # Per-filter match counts, grouped by filter type.
    filter_coverage: list[dict] = []
    for f in filters:
        label = f"{f['field']}:{f['value']}"
        count = sum(1 for r in run_rows if r.get("_matched_filter") == label)
        filter_coverage.append({"type": f["field"], "value": f["value"], "matches": count})

    summary = {
        "run_date": TODAY,
        "companies_total": companies_total,
        "companies_succeeded": companies_succeeded,
        "companies_failed": failed_companies,
        "filters_applied": [f"{f['field']}:{f['value']}" for f in filters],
        "filter_coverage": filter_coverage,
        "roles_fetched_post_filter": total,
        "field_population": {
            "description": populated("description"),
            "workplace_type": populated("workplace_type"),
            "compensation_raw": populated("compensation_raw"),
            "date_posted": populated("date_posted"),
            "department": populated("department"),
            "tier": populated("tier"),
        },
        "per_ats": per_ats,
        "per_company": company_stats,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch open job listings from companies.")
    parser.add_argument("--input", default="target_company_list.csv", help="Path to companies CSV (default: target_company_list.csv)")
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
    failed_companies: list[str] = []
    run_rows: list[dict] = []
    company_stats: list[dict] = []

    for i, entry in enumerate(companies):
        name = entry["company_name"]
        website = entry["website"]
        tier = entry.get("tier", "")
        co_stat: dict = {"company": name, "tier": tier, "ats": "", "roles_total": 0, "roles_post_filter": 0}

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
                co_stat["roles_total"] = len(rows)
                co_stat["ats"] = rows[0].get("_source", "") if rows else ""
                for row in rows:
                    row["tier"] = tier
                rows = apply_filters(rows, filters)
                co_stat["roles_post_filter"] = len(rows)
                # Deferred Ashby enrichment: only detail-fetch the rows that
                # survived filtering, to avoid rate-limiting on large boards.
                for row in rows:
                    if "_ashby_enrich" in row:
                        enrich_ashby_row(row, session)
                merge_rows(existing, rows, seen_ids)
                run_rows.extend(rows)
                total_roles += len(rows)
                succeeded += 1
            else:
                if args.verbose:
                    print("no roles found")
                failed_companies.append(name)
                co_stat["error"] = "No roles found via ATS or careers page"
                error_log.error(f"{name} | No roles found via ATS or careers page")

        except Exception as e:
            if args.verbose:
                print(f"ERROR: {e}")
            failed_companies.append(name)
            co_stat["error"] = str(e)
            error_log.error(f"{name} | {e}")

        company_stats.append(co_stat)

        if i < len(companies) - 1:
            time.sleep(args.delay)

    # Mark stale roles as no longer accepting
    for jid, row in existing.items():
        if jid not in seen_ids and row.get("accepting_applications") == "true":
            row["accepting_applications"] = "false"

    write_output(args.output, existing)
    write_run_summary(
        "last_run_summary.json",
        companies_total=len(companies),
        companies_succeeded=succeeded,
        failed_companies=failed_companies,
        filters=filters,
        run_rows=run_rows,
        company_stats=company_stats,
    )

    write_msg = f"Results written to {args.output}"
    fail_msg = f" | {len(failed_companies)} failed (see errors.log)" if failed_companies else ""
    print(f"\nDone! {total_roles} roles found across {succeeded}/{len(companies)} companies. "
          f"{write_msg}{fail_msg}")
    print("Run summary written to last_run_summary.json")


if __name__ == "__main__":
    main()
