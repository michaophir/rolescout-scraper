"""Text normalization helpers shared by the adapters (ported from scraper.py)."""

import html
import re

from bs4 import BeautifulSoup

WHITESPACE_RE = re.compile(r"\s+")


def collapse_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def strip_html(raw: str | None) -> str:
    """Decode HTML entities, strip tags, and collapse whitespace."""
    if not raw:
        return ""
    # Entities may be double-encoded (Greenhouse `content` uses &lt;p&gt;), so
    # unescape before parsing so tags become real tags.
    decoded = html.unescape(raw)
    text = BeautifulSoup(decoded, "html.parser").get_text(separator=" ")
    return collapse_whitespace(html.unescape(text))


def normalize_workplace_type(value: object) -> str:
    """Map a source's workplace value to remote / hybrid / onsite, else ""."""
    if not value:
        return ""
    s = str(value).strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    if s in {"remote", "fullyremote"}:
        return "remote"
    if s == "hybrid":
        return "hybrid"
    if s in {"onsite", "inoffice", "inperson"}:
        return "onsite"
    return ""


def html_to_lines(raw: str | None) -> str:
    """Like strip_html, but keeps one line per block (list item, paragraph)."""
    if not raw:
        return ""
    text = BeautifulSoup(html.unescape(raw), "html.parser").get_text(separator="\n")
    lines = (collapse_whitespace(line) for line in html.unescape(text).splitlines())
    return "\n".join(line for line in lines if line)
