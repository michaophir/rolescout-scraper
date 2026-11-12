import json
import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail any test that tries to open a network connection."""
    def blocked(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    """Stands in for requests.Session: serves one canned response, records URLs."""

    def __init__(self, payload=None, status_code=200):
        self.response = FakeResponse(payload, status_code)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.response


class FakeWeb:
    """A whole fake internet: serves pages and API responses by URL, 404 otherwise.

    ``pages`` maps a URL to an HTML string, a JSON-able object, or a
    ``(final_url, body)`` tuple to simulate a redirect.
    """

    def __init__(self, pages):
        self.pages = dict(pages)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url not in self.pages:
            return FakeWebResponse(url, None, 404)
        body = self.pages[url]
        if isinstance(body, tuple):
            url, body = body
        return FakeWebResponse(url, body, 200)


class FakeWebResponse:
    def __init__(self, url, body, status_code):
        self.url = url
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not JSON")
        return self._body


@pytest.fixture
def load_fixture():
    def load(name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return load


@pytest.fixture
def fake_session():
    return FakeSession


@pytest.fixture(autouse=True)
def isolated_db_path(monkeypatch, tmp_path):
    """Point the default database at a temp file so no test touches ~/.rolescout."""
    path = tmp_path / "rolescout-test.db"
    monkeypatch.setenv("ROLESCOUT_DB", str(path))
    return path


@pytest.fixture
def conn(isolated_db_path):
    from rolescout.db import connect
    connection = connect()
    yield connection
    connection.close()
