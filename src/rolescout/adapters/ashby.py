"""Ashby job boards (public posting API, api.ashbyhq.com/posting-api)."""

from rolescout.adapters.base import SourceAdapter, SourceError
from rolescout.models import Role
from rolescout.text import normalize_workplace_type, strip_html


class AshbyAdapter(SourceAdapter):
    name = "ashby"

    def fetch(self, ref: str, *, company: str | None = None) -> list[Role]:
        # One documented call returns description, workplace type, department,
        # published date and compensation for every posting on the board.
        url = f"https://api.ashbyhq.com/posting-api/job-board/{ref}?includeCompensation=true"
        return self.parse(self._get_json(url, ref), ref, company=company)

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        if not isinstance(payload, dict):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        roles = []
        for item in payload.get("jobs") or []:
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
