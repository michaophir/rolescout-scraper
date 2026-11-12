"""Lever job boards (api.lever.co postings API)."""

from datetime import datetime, timezone

from rolescout.adapters.base import SourceAdapter, SourceError
from rolescout.models import Role
from rolescout.text import normalize_workplace_type, strip_html


def _date_from_millis(value: object) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


class LeverAdapter(SourceAdapter):
    name = "lever"

    def fetch(self, ref: str, *, company: str | None = None) -> list[Role]:
        # The list call already carries description, comp text and workplace type.
        url = f"https://api.lever.co/v0/postings/{ref}?mode=json"
        return self.parse(self._get_json(url, ref), ref, company=company)

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        if not isinstance(payload, list):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        roles = []
        for item in payload:
            categories = item.get("categories") or {}
            roles.append(Role(
                source=self.name,
                source_job_id=item.get("id") or "",
                company=company or ref,
                title=(item.get("text") or "").strip(),
                department=categories.get("department") or "",
                location=categories.get("location") or "",
                workplace_type=normalize_workplace_type(item.get("workplaceType")),
                url=item.get("hostedUrl") or "",
                date_posted=_date_from_millis(item.get("createdAt")),
                compensation_text=(item.get("additionalPlain") or "").strip(),
                description=item.get("descriptionPlain") or strip_html(item.get("description")),
            ))
        return roles
