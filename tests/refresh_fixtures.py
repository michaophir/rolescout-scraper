"""Re-record the adapter fixtures from live boards, trimmed to a few roles.

    python tests/refresh_fixtures.py

Saves the raw source responses (not normalized output), so the tests exercise
the real parsing. After refreshing, update the expected values in
tests/test_adapters.py and tests/test_workday.py to match the newly recorded roles.

Workday takes many requests per board, so its fixtures are recordings of every
request the adapter made, one per second. Each is recorded from a board you
name and then anonymized: the tenant, the career site and the company's names
are replaced with acme, Careers and Acme before anything is written.

    python tests/refresh_fixtures.py workday paging URL --name COMPANY
    python tests/refresh_fixtures.py workday underscore-tenant URL --name COMPANY
    python tests/refresh_fixtures.py workday capped URL --name COMPANY

``paging`` wants a small board of 21 to 40 roles; ``underscore-tenant`` one
whose hostname has a hyphen where its tenant has an underscore; ``capped`` one
with more than 2,000 roles. Pass ``--name`` once for every name the company
goes by (brands and former names included). Read the result before committing
it: job titles, requisition numbers and description prose stay as recorded.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from rolescout.adapters import FetchContext, KnownRole, WorkdayAdapter
from rolescout.adapters.workday import WorkdayRef, parse_ref
from rolescout.http import DEFAULT_TIMEOUT, make_session

FIXTURES = Path(__file__).parent / "fixtures"
KEEP = 3

# fixture file -> (url, key holding the list of roles, or None if top-level)
BOARDS = {
    "greenhouse_anthropic.json": (
        "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true", "jobs"),
    "lever_spotify.json": (
        "https://api.lever.co/v0/postings/spotify?mode=json", None),
    "ashby_ramp.json": (
        "https://api.ashbyhq.com/posting-api/job-board/ramp?includeCompensation=true", "jobs"),
}


def main() -> None:
    session = make_session()
    for filename, (url, key) in BOARDS.items():
        resp = session.get(url, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        total = len(data[key] if key else data)
        if key:
            data[key] = data[key][:KEEP]
            if isinstance(data.get("meta"), dict):
                data["meta"]["total"] = KEEP
        else:
            data = data[:KEEP]
        (FIXTURES / filename).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{filename}: kept {KEEP} of {total} roles")


# --- Workday: record every exchange of a few fetches, then anonymize ----------

WORKDAY_WANTED = "manager"      # titles the recorded fetch wants descriptions for
WORKDAY_DESCRIPTIONS = 3
NEUTRAL_SITE = "Careers"
NEUTRAL_DATA_CENTER = "wd5"
NEUTRAL_NAME = "Acme"


def neutral_tenant(tenant: str) -> str:
    """acme, or acme_corp for a tenant that has an underscore (a hyphen in its hostname)."""
    return "acme_corp" if "_" in tenant or "-" in tenant else "acme"


def _same_case(found: str, word: str) -> str:
    return word.lower() if found.islower() else word.upper() if found.isupper() and len(found) > 1 else word


def anonymize(data, ref: WorkdayRef, names: list[str]):
    """``data`` with the board's tenant, site and company names replaced.

    ``ref`` is the board as recorded, with the tenant spelling that answered.
    Works on the JSON text, so URLs, ids, titles and description text all
    change the same way and stay consistent with each other.
    """
    text = json.dumps(data, ensure_ascii=False)
    tenant = neutral_tenant(ref.tenant)
    spellings = sorted({ref.tenant, ref.tenant.replace("_", "-"), ref.tenant.replace("-", "_")},
                       key=len, reverse=True)
    host = re.escape(ref.host)
    new_host = f"{tenant.replace('_', '-')}.{NEUTRAL_DATA_CENTER}.myworkdayjobs.com"
    site = re.escape(ref.site)
    # The site, wherever the structure says "this is the site".
    for spelling in spellings:
        new = tenant.replace("_", "-") if "-" in spelling else tenant
        text = re.sub(rf"/wday/cxs/{re.escape(spelling)}/{site}(?![\w-])", f"/wday/cxs/{new}/{NEUTRAL_SITE}",
                      text, flags=re.I)
        text = re.sub(rf"/recruiting/{re.escape(spelling)}/{site}(?![\w-])",
                      f"/recruiting/{new}/{NEUTRAL_SITE}", text, flags=re.I)
    text = re.sub(rf"({host})/{site}(?![\w-])", rf"\1/{NEUTRAL_SITE}", text, flags=re.I)
    # A role id is "host-label/site/anchor".
    label = re.escape(ref.tenant.replace("_", "-"))
    text = re.sub(rf'(?<=")({label})/{re.escape(ref.site.lower())}/',
                  rf"\1/{NEUTRAL_SITE.lower()}/", text)
    text = re.sub(rf"((?:Allow|Disallow): )/{site}/", rf"\1/{NEUTRAL_SITE}/", text)
    text = re.sub(rf'("jobPostingSiteId": )"{site}"', rf'\1"{NEUTRAL_SITE}"', text)
    text = re.sub(host, new_host, text, flags=re.I)
    # Then the tenant and the company's names, as words.
    for spelling in spellings:
        new = tenant.replace("_", "-") if "-" in spelling else tenant
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(spelling)}(?![A-Za-z0-9])", new, text)
    for name in sorted(names, key=len, reverse=True):
        words = r"[\s_-]*".join(re.escape(w) for w in name.split())
        text = re.sub(rf"(?<![A-Za-z0-9])(?i:{words})(?![a-z0-9])",
                      lambda m: _same_case(m.group(0), NEUTRAL_NAME), text)
    return json.loads(text)


def anonymize_facets(payload: dict) -> None:
    """Street addresses in a location facet name the company's offices: number them."""
    for facet in payload.get("facets") or []:
        for group in facet.get("values") or []:
            for n, value in enumerate(group.get("values") or [], 1):
                value["descriptor"] = f"Location {n}"


class Recorder:
    """Wraps a session and keeps each distinct exchange once."""

    def __init__(self, session) -> None:
        self.session = session
        self.exchanges: list[dict] = []

    def request(self, method, url, **kwargs):
        resp = self.session.request(method, url, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        exchange = {"method": method, "url": url, "json": kwargs.get("json"),
                    "status": resp.status_code, "body": body}
        if not any((e["method"], e["url"], e["json"]) == (method, url, exchange["json"])
                   for e in self.exchanges):
            self.exchanges.append(exchange)
        return resp


def write_fixture(filename: str, data) -> None:
    (FIXTURES / filename).write_text(
        json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{filename}: written")


def wants(title: str) -> bool:
    return WORKDAY_WANTED in title.lower()


def record_workday(kind: str, board: str, names: list[str]) -> None:
    session = make_session(min_interval=1.0)
    rec = Recorder(session)
    adapter = WorkdayAdapter(rec, max_descriptions=WORKDAY_DESCRIPTIONS)
    today = datetime.now(timezone.utc).date().isoformat()

    if kind == "capped":
        # The first page of a board over the cap, postings trimmed.
        first = adapter.fetch_raw(board)
        if first.get("total", 0) < 2000:
            sys.exit(f"{board} reports {first.get('total')} roles: not over the cap")
        first["jobPostings"] = first["jobPostings"][:KEEP]
        anonymize_facets(first)
        write_fixture("workday_capped_first_page.json", anonymize(first, adapter._ref(board), names))
        return

    extra = {}
    if kind == "paging":
        # 1. A whole small board, with descriptions for a few wanted titles.
        roles = adapter.fetch(board, context=FetchContext(wants_description=wants))
        # 2. One role marked saved whose title is not wanted.
        saved = next(r for r in roles if not wants(r.title))
        WorkdayAdapter(rec).fetch(board, context=FetchContext(
            known={saved.source_job_id: KnownRole(keep=True)}))
        # 3. The same board read as if a search stopped at 20 results.
        WorkdayAdapter(rec, result_cap=20).fetch(board)
        extra = {"saved_role": saved.source_job_id, "wanted": WORKDAY_WANTED,
                 "descriptions": WORKDAY_DESCRIPTIONS}
        filename = "workday_paging.json"
    else:
        # A tenant whose name differs from its hostname: a 422, then the retry.
        adapter.fetch(board)
        if not any(e["status"] == 422 for e in rec.exchanges):
            sys.exit(f"{board} answered under its hostname's spelling: no retry to record")
        filename = "workday_underscore_tenant.json"
    answered = adapter._ref(board)        # with the tenant spelling that answered
    neutral = WorkdayRef(neutral_tenant(answered.tenant), NEUTRAL_DATA_CENTER, NEUTRAL_SITE)
    write_fixture(filename, anonymize(
        {"recorded_on": today, "board": str(neutral), **extra, "exchanges": rec.exchanges},
        answered, names))


if __name__ == "__main__":
    if sys.argv[1:2] == ["workday"]:
        parser = argparse.ArgumentParser(prog="refresh_fixtures.py workday")
        parser.add_argument("kind", choices=["paging", "underscore-tenant", "capped"])
        parser.add_argument("board", help="the board to record from: any URL on its career site")
        parser.add_argument("--name", action="append", default=[], required=True,
                            help="a name the company goes by; repeat for each")
        args = parser.parse_args(sys.argv[2:])
        parse_ref(args.board)             # fail before any request on a bad reference
        record_workday(args.kind, args.board, args.name)
    else:
        main()
