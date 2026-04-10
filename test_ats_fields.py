"""Test script to inspect raw JSON responses from Greenhouse, Lever, and Ashby ATS endpoints."""

import json
import sys

import requests

TIMEOUT = 30


def print_header(title: str) -> None:
    bar = "=" * 80
    print(f"\n{bar}\n{title}\n{bar}")


def dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def summarize_fields(title: str, obj: dict) -> None:
    print(f"\n--- {title}: top-level fields ---")
    for key in obj.keys():
        print(f"  - {key}")


def test_greenhouse() -> None:
    print_header("GREENHOUSE (anthropic)")
    list_url = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"
    resp = requests.get(list_url, timeout=TIMEOUT)
    resp.raise_for_status()
    listing = resp.json()

    jobs = listing.get("jobs", [])
    if not jobs:
        print("No jobs returned from Greenhouse listing.")
        return

    first = jobs[0]
    job_id = first.get("id")
    print(f"Picked first job id={job_id} title={first.get('title')!r}")

    detail_url = f"https://boards-api.greenhouse.io/v1/boards/anthropic/jobs/{job_id}"
    detail_resp = requests.get(detail_url, timeout=TIMEOUT)
    detail_resp.raise_for_status()
    detail = detail_resp.json()

    print("\n--- Full Greenhouse job detail JSON ---")
    dump(detail)
    summarize_fields("Greenhouse job detail", detail)


def test_lever() -> None:
    print_header("LEVER (spotify)")
    url = "https://api.lever.co/v0/postings/spotify?mode=json"
    resp = requests.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    postings = resp.json()

    if not postings:
        print("No postings returned from Lever.")
        return

    first = postings[0]
    print(f"Picked first posting id={first.get('id')} title={first.get('text')!r}")

    print("\n--- Full Lever posting JSON ---")
    dump(first)
    summarize_fields("Lever posting", first)


ASHBY_GRAPHQL_URL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"

ASHBY_JOB_BOARD_QUERY = """
query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {
  jobBoard: jobBoardWithTeams(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
  ) {
    teams { id name parentTeamId }
    jobPostings {
      id
      title
      teamId
      locationId
      locationName
      employmentType
      secondaryLocations { locationId locationName }
      compensationTierSummary
    }
  }
}
"""

ASHBY_JOB_POSTING_QUERY = """
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(
    organizationHostedJobsPageName: $organizationHostedJobsPageName
    jobPostingId: $jobPostingId
  ) {
    id
    title
    departmentName
    departmentExternalName
    teamNames
    locationName
    locationAddress
    workplaceType
    employmentType
    descriptionHtml
    linkedData
    isListed
    isConfidential
    publishedDate
    applicationDeadline
    secondaryLocationNames
    compensationTierSummary
    compensationTierGuideUrl
    compensationPhilosophyHtml
    scrapeableCompensationSalarySummary
    applicationLimitCalloutHtml
    shouldAskForTextingConsent
    legalEntityNameForTextingConsent
  }
}
"""


def test_ashby() -> None:
    print_header("ASHBY (notion)")
    org = "notion"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    list_payload = {
        "operationName": "ApiJobBoardWithTeams",
        "variables": {"organizationHostedJobsPageName": org},
        "query": ASHBY_JOB_BOARD_QUERY,
    }
    list_url = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams"
    list_resp = requests.post(list_url, json=list_payload, headers=headers, timeout=TIMEOUT)
    list_resp.raise_for_status()
    list_data = list_resp.json()

    postings = (
        list_data.get("data", {}).get("jobBoard", {}).get("jobPostings", [])
    )
    if not postings:
        print("No postings returned from Ashby job board.")
        print("Response:")
        dump(list_data)
        return

    first = postings[0]
    job_id = first.get("id")
    print(f"Picked first posting id={job_id} title={first.get('title')!r}")

    detail_payload = {
        "operationName": "ApiJobPosting",
        "variables": {
            "organizationHostedJobsPageName": org,
            "jobPostingId": job_id,
        },
        "query": ASHBY_JOB_POSTING_QUERY,
    }
    detail_resp = requests.post(
        ASHBY_GRAPHQL_URL, json=detail_payload, headers=headers, timeout=TIMEOUT
    )
    detail_resp.raise_for_status()
    detail_data = detail_resp.json()

    print("\n--- Full Ashby jobPosting JSON ---")
    dump(detail_data)

    posting = detail_data.get("data", {}).get("jobPosting")
    if isinstance(posting, dict):
        summarize_fields("Ashby jobPosting", posting)


def main() -> int:
    for fn in (test_greenhouse, test_lever, test_ashby):
        try:
            fn()
        except requests.HTTPError as e:
            print(f"\nHTTP error in {fn.__name__}: {e}")
        except requests.RequestException as e:
            print(f"\nRequest error in {fn.__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
