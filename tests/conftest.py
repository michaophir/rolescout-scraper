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


@pytest.fixture
def load_fixture():
    def load(name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return load


@pytest.fixture
def fake_session():
    return FakeSession
