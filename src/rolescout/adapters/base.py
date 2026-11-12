"""The adapter interface every source implements."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

import requests

from rolescout.http import DEFAULT_TIMEOUT, make_session
from rolescout.models import Role


class SourceError(Exception):
    """The source could not be fetched or returned something unexpected."""


class BoardNotFound(SourceError):
    """The source has no board for the given reference."""


@dataclass(frozen=True)
class BoardIdentity:
    """What a board says about who owns it. Either field may be ""."""

    name: str = ""
    website: str = ""


class SourceAdapter(ABC):
    """Fetches every open role for one source reference.

    ``ref`` identifies a board within the source. For Greenhouse, Lever and
    Ashby it is the board slug. Adapters do no filtering and no scoring: an
    empty list means the board exists and has no open roles; a board that
    cannot be fetched raises ``SourceError`` (``BoardNotFound`` for a 404).
    """

    name: ClassVar[str]

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or make_session()

    def fetch(self, ref: str, *, company: str | None = None) -> list[Role]:
        """Return every open role on the board.

        ``company`` sets the display name on each role. Without it the adapter
        uses the name the source reports, or ``ref`` if it reports none.
        """
        return self.parse(self.fetch_raw(ref), ref, company=company)

    def fetch_raw(self, ref: str) -> Any:
        """The source's own JSON for the board, unparsed."""
        return self._get_json(self.board_url(ref), ref)

    @abstractmethod
    def board_url(self, ref: str) -> str:
        """The one URL that returns every open role on the board."""

    @abstractmethod
    def items(self, payload: Any, ref: str) -> list[dict]:
        """The raw posting objects inside a ``fetch_raw`` payload."""

    @abstractmethod
    def parse(self, payload: Any, ref: str, *, company: str | None = None) -> list[Role]:
        """Normalize a ``fetch_raw`` payload."""

    @abstractmethod
    def identity(self, ref: str) -> BoardIdentity:
        """Who the board says it belongs to. Costs one extra request.

        Used only by ``companies resolve`` to check a guessed slug.
        """

    def _get_text(self, url: str, ref: str) -> str:
        try:
            resp = self.session.get(url, timeout=DEFAULT_TIMEOUT, headers={"Accept": "text/html"})
        except requests.RequestException as exc:
            raise SourceError(f"{self.name}: request for {ref!r} failed: {exc}") from exc
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: HTTP {resp.status_code} for {ref!r}")
        return resp.text

    def _get_json(self, url: str, ref: str) -> Any:
        try:
            resp = self.session.get(url, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise SourceError(f"{self.name}: request for {ref!r} failed: {exc}") from exc
        if resp.status_code == 404:
            raise BoardNotFound(f"{self.name}: no board found for {ref!r}")
        if resp.status_code != 200:
            raise SourceError(f"{self.name}: HTTP {resp.status_code} for {ref!r}")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError(f"{self.name}: non-JSON response for {ref!r}") from exc
