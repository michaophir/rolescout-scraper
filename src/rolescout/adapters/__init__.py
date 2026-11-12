"""Source adapters. One module per source, all behind ``SourceAdapter``."""

import requests

from rolescout.adapters.ashby import AshbyAdapter
from rolescout.adapters.base import BoardIdentity, BoardNotFound, SourceAdapter, SourceError
from rolescout.adapters.greenhouse import GreenhouseAdapter
from rolescout.adapters.lever import LeverAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    cls.name: cls for cls in (GreenhouseAdapter, LeverAdapter, AshbyAdapter)
}


def get_adapter(source: str, session: requests.Session | None = None) -> SourceAdapter:
    try:
        return ADAPTERS[source](session)
    except KeyError:
        raise ValueError(
            f"unknown source {source!r}; expected one of {', '.join(sorted(ADAPTERS))}"
        ) from None


__all__ = [
    "ADAPTERS", "BoardIdentity", "BoardNotFound", "SourceAdapter", "SourceError", "get_adapter",
    "AshbyAdapter", "GreenhouseAdapter", "LeverAdapter",
]
