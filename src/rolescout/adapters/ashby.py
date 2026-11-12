"""Ashby job boards (public posting API, api.ashbyhq.com/posting-api)."""

import json
import re

from rolescout.adapters.base import BoardIdentity, SourceAdapter, SourceError
from rolescout.models import Role
from rolescout.text import normalize_workplace_type, strip_html


class AshbyAdapter(SourceAdapter):
    name = "ashby"

    def board_url(self, ref: str) -> str:
        # One documented call returns description, workplace type, department,
        # published date and compensation for every posting on the board.
        return f"https://api.ashbyhq.com/posting-api/job-board/{ref}?includeCompensation=true"

    def items(self, payload: object, ref: str) -> list[dict]:
        if not isinstance(payload, dict):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        return payload.get("jobs") or []

    def identity(self, ref: str) -> BoardIdentity:
        # The posting API names no organization; the hosted page embeds one,
        # including the website the company entered in Ashby.
        page = self._get_text(f"https://jobs.ashbyhq.com/{ref}", ref)

        def embedded(key: str) -> str:
            match = re.search(rf'"organization":\{{[^{{}}]*?"{key}":("(?:[^"\\]|\\.)*")', page)
            try:
                return (json.loads(match.group(1)) or "").strip() if match else ""
            except ValueError:
                return ""
        return BoardIdentity(name=embedded("name"), website=embedded("publicWebsite"))

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        roles = []
        for item in self.items(payload, ref):
            job_id = item.get("id") or ""
            # Unlisted postings are reachable by direct link only; the board
            # itself does not show them, so neither do we.
            if not job_id or item.get("isListed") is False:
                continue
            compensation = item.get("compensation") or {}
            roles.append(Role(
                source=self.name,
                source_job_id=job_id,
                company=company or ref,
                title=(item.get("title") or "").strip(),
                department=item.get("department") or "",
                location=item.get("location") or "",
                workplace_type=normalize_workplace_type(item.get("workplaceType")),
                url=item.get("jobUrl") or f"https://jobs.ashbyhq.com/{ref}/{job_id}",
                date_posted=(item.get("publishedAt") or "")[:10],
                compensation_text=(compensation.get("compensationTierSummary") or "").strip(),
                description=strip_html(item.get("descriptionHtml")),
            ))
        return roles
