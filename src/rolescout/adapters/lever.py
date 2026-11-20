"""Lever job boards (api.lever.co postings API)."""

import html
import re
from datetime import datetime, timezone

from rolescout.adapters.base import BoardIdentity, SourceAdapter, SourceError
from rolescout.models import Role
from rolescout.text import html_to_lines, normalize_workplace_type, pay_text, strip_html


def _date_from_millis(value: object) -> str:
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _amount(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pay(item: dict) -> str:
    """Pay, from the two fields a posting has for it and no other.

    ``salaryRange`` holds the numbers (``min``, ``max``, ``currency`` and an
    ``interval`` such as ``per-year-salary``); ``salaryDescription`` is the
    company's sentence about them. Few postings fill either in. Pay written
    anywhere else is prose among other prose and stays in the description:
    ``additional``, the closing section, mostly holds benefits and the
    equal-opportunity statement.
    """
    pay = item.get("salaryRange")
    pay = pay if isinstance(pay, dict) else {}
    low, high = _amount(pay.get("min")), _amount(pay.get("max"))
    if low or high:
        amount = f"{low:,.0f} - {high:,.0f}" if low and high and low != high else f"{(high or low):,.0f}"
        interval = str(pay.get("interval") or "").lower()
        period = re.match(r"per-([a-z]+)", interval)
        per = f"per {period.group(1)}" if period else interval.replace("-", " ")
        return " ".join(part for part in (str(pay.get("currency") or ""), amount, per) if part)
    return pay_text(item.get("salaryDescriptionPlain") or strip_html(item.get("salaryDescription")))


def _full_description(item: dict) -> str:
    """Opening text, then each list section under its heading, then `additional`.

    Lever splits a posting into these three parts; the opening alone omits
    the responsibilities and requirements.
    """
    parts = [(item.get("descriptionPlain") or strip_html(item.get("description"))).strip()]
    for section in item.get("lists") or []:
        heading = (section.get("text") or "").strip()
        parts.append(f"{heading}\n{html_to_lines(section.get('content'))}".strip())
    parts.append((item.get("additionalPlain") or strip_html(item.get("additional"))).strip())
    return "\n\n".join(part for part in parts if part)


class LeverAdapter(SourceAdapter):
    name = "lever"

    def board_url(self, ref: str) -> str:
        # The list call already carries description, pay and workplace type.
        return f"https://api.lever.co/v0/postings/{ref}?mode=json"

    def items(self, payload: object, ref: str) -> list[dict]:
        if not isinstance(payload, list):
            raise SourceError(f"{self.name}: unexpected response shape for {ref!r}")
        return payload

    def identity(self, ref: str) -> BoardIdentity:
        # The API has no company fields; the hosted page has a title and a
        # logo link that some companies point at their own site.
        page = self._get_text(f"https://jobs.lever.co/{ref}", ref)
        title = re.search(r"<title>(.*?)</title>", page, re.S)
        logo = re.search(r'<a[^>]*href="([^"]+)"[^>]*class="main-header-logo"', page)
        website = html.unescape(logo.group(1)) if logo else ""
        if "lever.co" in website:
            website = ""
        return BoardIdentity(
            name=html.unescape(title.group(1)).strip() if title else "", website=website)

    def parse(self, payload: object, ref: str, *, company: str | None = None) -> list[Role]:
        roles = []
        for item in self.items(payload, ref):
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
                compensation_text=_pay(item),
                description=_full_description(item),
            ))
        return roles
