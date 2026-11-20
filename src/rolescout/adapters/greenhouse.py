"""Greenhouse job boards (boards-api.greenhouse.io)."""

import html

from bs4 import BeautifulSoup

from rolescout.adapters.base import BoardIdentity, SourceAdapter, SourceError
from rolescout.models import Role
from rolescout.text import pay_text, strip_html

COMPENSATION_BLOCK_MAX_CHARS = 500


def extract_compensation(content_html: str) -> str:
    """Pay from a Greenhouse `content` HTML blob: the pay range the board
    marks up, else its pay-transparency block. Either counts only when it
    names a figure."""
    if not content_html:
        return ""
    soup = BeautifulSoup(html.unescape(content_html), "html.parser")
    for selector in (".pay-range", ".content-pay-transparency"):
        found = soup.select_one(selector)
        text = pay_text(found.get_text(separator=" "), COMPENSATION_BLOCK_MAX_CHARS) if found else ""
        if text:
            return text
    return ""


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
