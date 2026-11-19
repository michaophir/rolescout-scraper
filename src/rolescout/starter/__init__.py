"""The starter set: what a new install begins with.

- ``companies.json``: well-known technology companies with their websites
  and job boards, in the format ``rolescout companies export`` writes. Every
  board in it was confirmed live when the file was made: it answered, listed
  open roles, and named the company.
- ``portfolio_boards.json``: a few public portfolio job boards, as an
  example of the kind ``rolescout portfolio add`` takes.

It is a starting point for product and engineering job searches, not
anyone's own list. ``rolescout init`` loads it; everything in it can be
changed afterwards with the ordinary commands.
"""

import json
from importlib import resources


def _load(name: str):
    return json.loads(resources.files(__package__).joinpath(name).read_text(encoding="utf-8"))


def companies() -> dict:
    """The starter companies and boards, as a company export."""
    return _load("companies.json")


def portfolio_boards() -> list[dict]:
    """The example portfolio boards: ``{"url": ..., "name": ...}`` each."""
    return _load("portfolio_boards.json")
