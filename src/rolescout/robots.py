"""robots.txt, evaluated as RFC 9309 says.

The standard library's parser is not used: before Python 3.14 it obeys the
first rule that matches, drops the query from a rule's path and knows no
wildcards, so the answer depended on the Python that ran it.

The rules that matter (RFC 9309, section 2.2):

- The group for our product token applies; if there is none, the ``*``
  group does. Several groups for the same agent count as one.
- Of the rules that match a path, the one with the longest pattern wins.
  When an ``Allow`` and a ``Disallow`` are equally long, ``Allow`` wins.
- ``*`` in a pattern stands for any run of characters, and ``$`` at its end
  for the end of the path. Every other pattern matches as a prefix.
- A path no rule matches is allowed, and so is ``/robots.txt`` itself.
- The path compared is the URL's path and query.

What to do with a missing or failing robots.txt is the caller's decision;
``ALLOW_ALL`` and ``DISALLOW_ALL`` are there for it.
"""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_TOKEN = re.compile(r"[A-Za-z_-]+")
MAX_CHARS = 500_000     # the part of a file that is read; the RFC asks for at least 500 KiB


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str        # as compared: escapes normalized
    line: int           # where it stands in the file, from 1
    text: str           # as written, e.g. "Disallow: /private"

    def matches(self, path: str) -> bool:
        return _matches(self.pattern, path)


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    rule: Rule | None   # the rule that decided; None when no rule matched

    def __bool__(self) -> bool:
        return self.allowed

    def reason(self) -> str:
        """For a message: which line of the file decided."""
        return f"line {self.rule.line}: {self.rule.text}" if self.rule else "no rule matches"


def _normalize(text: str) -> str:
    """One spelling for a path or pattern: characters outside ASCII become
    percent-escapes, escapes of unreserved characters become the characters,
    and the escapes that stay are in capitals."""
    text = "".join(ch if ord(ch) < 128 else "".join(f"%{b:02X}" for b in ch.encode("utf-8")) for ch in text)

    def escape(match: re.Match) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else match.group(0).upper()

    return _ESCAPE.sub(escape, text)


def _matches(pattern: str, path: str) -> bool:
    """Whether a rule's pattern matches a path. No backtracking: each piece
    between two ``*`` is looked for once, as early as it can be found."""
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    first, *rest = pattern.split("*")
    if not path.startswith(first):
        return False
    if not rest:
        return len(path) == len(first) if anchored else True
    at = len(first)
    last = rest.pop()
    for piece in rest:
        found = path.find(piece, at)
        if found < 0:
            return False
        at = found + len(piece)
    if anchored:
        return len(path) - len(last) >= at and path.endswith(last)
    return path.find(last, at) >= 0


def product_token(user_agent: str) -> str:
    """``rolescout/1.0 (+https://...)`` -> ``rolescout``."""
    match = _TOKEN.match(user_agent.strip())
    return match.group(0).lower() if match else ""


def path_of(url: str) -> str:
    """The part of a URL that rules are compared with: path and query."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return _normalize(path + ("?" + parts.query if parts.query else ""))


class Robots:
    """The rules of one robots.txt."""

    def __init__(self, groups: dict[str, list[Rule]] | None = None) -> None:
        self._groups = groups or {}     # by agent token in lower case; "*" is everyone else

    @classmethod
    def parse(cls, text: str) -> "Robots":
        groups: dict[str, list[Rule]] = {}
        agents: list[str] = []          # the agents of the group being read
        in_rules = False                # True once that group has a rule
        for number, raw in enumerate((text or "")[:MAX_CHARS].lstrip("﻿").splitlines(), start=1):
            line = raw.split("#", 1)[0].strip()
            key, colon, value = line.partition(":")
            if not colon:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if in_rules:
                    agents, in_rules = [], False
                match = _TOKEN.match(value)
                agent = "*" if value.startswith("*") else (match.group(0).lower() if match else "")
                if agent:
                    agents.append(agent)
                    groups.setdefault(agent, [])
            elif key in ("allow", "disallow"):
                in_rules = True
                if not value:
                    continue            # "Disallow:" with nothing after it disallows nothing
                rule = Rule(key == "allow", _normalize(value), number, f"{key.capitalize()}: {value}")
                for agent in agents:
                    groups[agent].append(rule)
            # Any other line (Sitemap, Crawl-delay) neither adds to a group nor ends it.
        return cls(groups)

    def rules_for(self, user_agent: str) -> list[Rule]:
        token = product_token(user_agent)
        if token in self._groups:
            return self._groups[token]
        return self._groups.get("*", [])

    def check(self, user_agent: str, url: str) -> Verdict:
        """Whether ``user_agent`` may fetch ``url``, and the rule that says so."""
        path = path_of(url)
        if path == "/robots.txt":
            return Verdict(True, None)
        best: Rule | None = None
        for rule in self.rules_for(user_agent):
            if not rule.matches(path):
                continue
            if best is None or len(rule.pattern) > len(best.pattern) \
                    or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow):
                best = rule
        return Verdict(best is None or best.allow, best)

    def allows(self, user_agent: str, url: str) -> bool:
        return self.check(user_agent, url).allowed


ALLOW_ALL = Robots()
DISALLOW_ALL = Robots.parse("User-agent: *\nDisallow: /")
