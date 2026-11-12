"""Shared HTTP session: an honest User-Agent and a polite request rate."""

import time

import requests

from rolescout import __version__

REPO_URL = "https://github.com/michaophir/rolescout-scraper"
USER_AGENT = f"rolescout/{__version__} (+{REPO_URL})"
DEFAULT_TIMEOUT = 30  # seconds; full-board payloads run to several MB


class PoliteSession(requests.Session):
    """A session that leaves at least ``min_interval`` seconds between requests."""

    def __init__(self, min_interval: float = 0.0) -> None:
        super().__init__()
        self.min_interval = min_interval
        self._last_request: float | None = None

    def request(self, *args, **kwargs):
        if self.min_interval > 0 and self._last_request is not None:
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
        try:
            return super().request(*args, **kwargs)
        finally:
            # Measured from the end of the previous request, so a slow
            # response never shortens the pause before the next one.
            self._last_request = time.monotonic()


def make_session(min_interval: float = 0.0) -> requests.Session:
    session = PoliteSession(min_interval)
    session.headers["User-Agent"] = USER_AGENT
    session.headers["Accept"] = "application/json"
    return session
