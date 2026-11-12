"""Greenhouse job boards (boards-api.greenhouse.io)."""

import html

from bs4 import BeautifulSoup

from rolescout.adapters.base import SourceAdapter, SourceError
from rolescout.models import Role
from rolescout.text import collapse_whitespace, strip_html

COMPENSATION_BLOCK_MAX_CHARS = 500


def extract_compensation(content_html: str) -> str:
    """Pull compensation text out of a Greenhouse `content` HTML blob."""
    if not content_html:
        return ""
    soup = BeautifulSoup(html.unescape(content_html), "html.parser")
    pay_range = soup.select_one(".pay-range")
    if pay_range:
        text = collapse_whitespace(pay_range.get_text(separator=" "))
        if text:
            return text
    pay_block = soup.select_one(".content-pay-transparency")
    if pay_block:
        text = collapse_whitespace(pay_block.get_text(separator=" "))
        if text:
            return text[:COMPENSATION_BLOCK_MAX_CHARS]
    return ""


class GreenhouseAdapter(SourceAdapter):
    name = "greenhouse"

    def fetch(self, ref: str, *, company: str | None = None) -> list[Role]:
        # ?content=true returns each job's description in the one list call.
        url = f"https://boards-api.greenhouse.io/v1/boards/{ref}/jobs?content=true"
        return self.parse(self._get_json(url, ref), ref, company=company)

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        if not isinstance(payload, dict):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        roles = []
        for item in payload.get("jobs") or []:
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
