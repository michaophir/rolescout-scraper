"""The adapter interface every source implements."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlsplit

import requests

from rolescout.http import DEFAULT_TIMEOUT, USER_AGENT, make_session
from rolescout.models import Role
from rolescout.robots import ALLOW_ALL, Robots, Verdict


class SourceError(Exception):
    """The source could not be fetched or returned something unexpected."""


class BoardNotFound(SourceError):
    """The source has no board for the given reference."""


@dataclass(frozen=True)
class BoardIdentity:
    """What a board says about who owns it. Either field may be ""."""

    name: str = ""
    website: str = ""


@dataclass(frozen=True)
class KnownRole:
    """What is already stored for a role the board listed before."""

    date_posted: str = ""
    location: str = ""
    workplace_type: str = ""
    has_description: bool = False
    keep: bool = False           # marked saved or applied


def _wants_nothing(title: str) -> bool:
    return False


@dataclass(frozen=True)
class FetchContext:
    """What the caller knows before a fetch. Only sources where a description
    costs one request per role read it (``SourceAdapter.uses_context``)."""

    # Whether a role with this title should get its description fetched.
    wants_description: Callable[[str], bool] = _wants_nothing
    known: Mapping[str, KnownRole] = field(default_factory=dict)   # by source_job_id


# A read that saw at least this share of the roles the site says it lists
# counts as complete. Long lists move while they are read, and some sites
# report a few more roles than they return.
COMPLETE_SHARE = 0.99


@dataclass
class FetchStats:
    """What one ``fetch`` cost and whether it saw the whole board."""

    requests: int = 0
    # False when the fetch stopped early. The roles returned are real, but a
    # role missing from them must not be read as closed.
    complete: bool = True
    descriptions_fetched: int = 0
    descriptions_waiting: int = 0    # wanted, left for a later fetch
    notes: list[str] = field(default_factory=list)
    # For a source that reports how many roles it lists: how many of them
    # the fetch saw, and that reported number. None when it reports none.
    seen: int | None = None
    reported: int | None = None

    def count(self, seen: int, reported: int) -> None:
        """Record what was seen against what the site reports. A read under
        ``COMPLETE_SHARE`` of the reported number is not complete."""
        self.seen, self.reported = seen, reported
        if reported > 0 and seen < COMPLETE_SHARE * reported:
            self.complete = False
            self.notes.append(f"saw {seen} of the {reported} roles listed "
                              f"({100 * seen / reported:.1f}%, under {COMPLETE_SHARE:.0%})")


class SourceAdapter(ABC):
    """Fetches every open role for one source reference.

    ``ref`` identifies a board within the source. For most sources it is the
    board slug; for Workday it is tenant/data-center/site.
    Adapters do no filtering and no scoring: an empty list means the board
    exists and has no open roles; a board that cannot be fetched raises
    ``SourceError`` (``BoardNotFound`` for a 404).
    """

    name: ClassVar[str]
    # True when ``fetch`` reads its ``context`` (descriptions cost extra requests).
    uses_context: ClassVar[bool] = False
    # False when a board cannot be found by trying a slug made from the company name.
    guessable: ClassVar[bool] = True
    # True when a slug made from the company name is worth trying once the
    # company's own site shows it uses this source, though not otherwise.
    guess_when_named: ClassVar[bool] = False

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or make_session()
        self.last_stats: FetchStats | None = None   # set by every ``fetch``
        self._robots: dict[str, Robots] = {}   # by host

    def fetch(self, ref: str, *, company: str | None = None,
              context: FetchContext | None = None) -> list[Role]:
        """Return every open role on the board.

        ``company`` sets the display name on each role. Without it the adapter
        uses the name the source reports, or ``ref`` if it reports none.
        """
        self.last_stats = FetchStats(requests=1)
        return self.parse(self.fetch_raw(ref), ref, company=company)

    @classmethod
    def canonical_ref(cls, ref: str) -> str:
        """The one stored spelling of a board reference. Raises ``ValueError``
        for text that cannot be a reference for this source."""
        return ref.strip()

    def confirm(self, ref: str) -> tuple[str, int]:
        """Check that a board exists: (the reference to store, open roles).

        Raises ``BoardNotFound`` when it does not. Used by ``companies resolve``.
        """
        self.last_stats = FetchStats(requests=1)
        return self.canonical_ref(ref), len(self.items(self.fetch_raw(ref), ref))

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

    def _check_robots(self, url: str, ref: str) -> None:
        """Raise ``SourceError`` when the host's robots.txt disallows ``url``.

        The file is read once per host for the life of the adapter and
        evaluated as RFC 9309 says (see ``rolescout.robots``). A missing or
        refused file (any 4xx) allows everything; a server error disallows
        everything.
        """
        verdict = self._robots_verdict(url)
        if not verdict:
            parts = urlsplit(url)
            raise SourceError(f"{self.name}: robots.txt on {parts.netloc} disallows {parts.path} "
                              f"(board {ref!r}; {verdict.reason()})")

    def _robots_verdict(self, url: str) -> Verdict:
        """What the host's robots.txt says about ``url``. Raises ``SourceError``
        when the file cannot be read."""
        parts = urlsplit(url)
        host = parts.netloc
        if host not in self._robots:
            if self.last_stats is not None:
                self.last_stats.requests += 1
            try:
                resp = self.session.get(f"{parts.scheme}://{host}/robots.txt", timeout=DEFAULT_TIMEOUT,
                                        headers={"Accept": "*/*"})
            except requests.RequestException as exc:
                raise SourceError(f"{self.name}: robots.txt for {host} could not be read: {exc}") from exc
            if resp.status_code >= 500:
                raise SourceError(f"{self.name}: robots.txt for {host} answered HTTP {resp.status_code}")
            self._robots[host] = Robots.parse(resp.text or "") if resp.status_code == 200 else ALLOW_ALL
        return self._robots[host].check(USER_AGENT, url)

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
