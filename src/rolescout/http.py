"""Shared HTTP session with an honest User-Agent."""

import requests

from rolescout import __version__

REPO_URL = "https://github.com/michaophir/rolescout-scraper"
USER_AGENT = f"rolescout/{__version__} (+{REPO_URL})"
DEFAULT_TIMEOUT = 30  # seconds; full-board payloads run to several MB


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Accept"] = "application/json"
    return session
