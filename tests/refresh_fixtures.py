"""Re-record the adapter fixtures from live boards, trimmed to a few roles.

    python tests/refresh_fixtures.py

Saves the raw source responses (not normalized output), so the tests exercise
the real parsing. After refreshing, update the expected values in
tests/test_adapters.py to match the newly recorded roles.
"""

import json
from pathlib import Path

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


if __name__ == "__main__":
    main()
