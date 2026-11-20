"""Greenhouse job boards (boards-api.greenhouse.io)."""

import html
import re

from bs4 import BeautifulSoup

from rolescout.adapters.base import BoardIdentity, SourceAdapter, SourceError
from rolescout.models import Role
from rolescout.text import collapse_whitespace, pay_text, strip_html

COMPENSATION_BLOCK_MAX_CHARS = 500
LEAD_IN_CHARS = 100        # of the text before the first figure, when the block is cut
_AMOUNT = re.compile(r"(?:[$£€]|\b(?:USD|CAD|GBP|EUR)\b)\s?\d")


def extract_compensation(content_html: str) -> str:
    """Pay from a Greenhouse `content` HTML blob: the pay ranges the board
    marks up, else its pay-transparency block. Either counts only when it
    names a figure.

    A board may give several ranges, one per pay zone or location, each in a
    ``.pay-input`` with a ``.title`` ("Zone A:") and a ``.pay-range``. Every
    one is kept, each after its title: "Zone A: $83,900 — $125,900 USD;
    Zone B: $78,100 — $117,100 USD". A single range is kept on its own.
    """
    if not content_html:
        return ""
    soup = BeautifulSoup(html.unescape(content_html), "html.parser")
    ranges = []
    for found in soup.select(".pay-range"):
        text = pay_text(found.get_text(separator=" "))
        if not text:
            continue
        block = found.find_parent(class_="pay-input")
        title = block.select_one(".title") if block is not None else None
        label = collapse_whitespace(title.get_text(separator=" ")).rstrip(" :") if title is not None else ""
        ranges.append((label, text))
    if len(ranges) == 1:
        return ranges[0][1]
    if ranges:
        parts = [f"{label}: {text}" if label else text for label, text in dict.fromkeys(ranges)]
        joined = ""
        for part in parts:         # whole ranges only, within the length kept
            candidate = f"{joined}; {part}" if joined else part
            if len(candidate) > COMPENSATION_BLOCK_MAX_CHARS:
                break
            joined = candidate
        return joined
    block = soup.select_one(".content-pay-transparency")
    if block is None:
        return ""
    text = collapse_whitespace(block.get_text(separator=" "))
    first = _AMOUNT.search(text)
    if first is not None and len(text) > COMPENSATION_BLOCK_MAX_CHARS and first.start() > LEAD_IN_CHARS:
        # The figures come after a long explanation: keep them, and a little
        # of what leads into them, rather than the explanation alone.
        start = text.rfind(" ", 0, first.start() - LEAD_IN_CHARS) + 1
        text = "…" + text[start:]
    return pay_text(text, COMPENSATION_BLOCK_MAX_CHARS)


class GreenhouseAdapter(SourceAdapter):
    name = "greenhouse"

    def board_url(self, ref: str) -> str:
        # ?content=true returns each job's description in the one list call.
        return f"https://boards-api.greenhouse.io/v1/boards/{ref}/jobs?content=true"

    def items(self, payload: object, ref: str) -> list[dict]:
        if not isinstance(payload, dict):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        return payload.get("jobs") or []

    def identity(self, ref: str) -> BoardIdentity:
        # The board endpoint carries a display name and nothing else useful.
        board = self._get_json(f"https://boards-api.greenhouse.io/v1/boards/{ref}", ref)
        return BoardIdentity(name=(board.get("name") or "").strip() if isinstance(board, dict) else "")

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        roles = []
        for item in self.items(payload, ref):
            location = (item.get("location") or {}).get("name") or ""
            departments = item.get("departments") or []
            content_html = item.get("content") or ""
            roles.append(Role(
                source=self.name,
                source_job_id=str(item.get("id", "")),
                company=company or item.get("company_name") or ref,
                title=(item.get("title") or "").strip(),
                department=(departments[0].get("name") or "") if departments else "",
                location=location,
                # Greenhouse has no workplace-type field; only "remote" can be
                # inferred, from the location text.
                workplace_type="remote" if "remote" in location.lower() else "",
                url=item.get("absolute_url") or "",
                date_posted=(item.get("first_published") or item.get("updated_at") or "")[:10],
                compensation_text=extract_compensation(content_html),
                description=strip_html(content_html),
            ))
        return roles
