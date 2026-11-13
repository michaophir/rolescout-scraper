"""Company website values: one normal form, checked wherever one enters the system."""

import re
from urllib.parse import urlsplit, urlunsplit

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class InvalidWebsite(ValueError):
    """The value cannot be a company website."""


def normalize_website(value: object) -> str:
    """A full URL for a company website, or "" for none.

    "alation.com", "www.alation.com/about" and "//alation.com" become
    https:// URLs; a value that already says http:// or https:// keeps its
    scheme. The host is lowercased. Anything that is not a web address with
    a dotted hostname is rejected with ``InvalidWebsite``.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if any(ch.isspace() for ch in text):
        raise InvalidWebsite(f"{text!r} is not a website: it contains a space")
    scheme = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*):(?!\d+(?:/|$))", text)   # "host:8080" is not a scheme
    if scheme and scheme.group(1).lower() not in ("http", "https"):
        raise InvalidWebsite(f"{text!r} is not a website: expected http or https, "
                             f"not {scheme.group(1).lower()}:")
    if not scheme:
        text = "https://" + text.lstrip("/")
    try:
        parts = urlsplit(text)
        host, port = parts.hostname or "", parts.port
    except ValueError:
        raise InvalidWebsite(f"{value!r} is not a website") from None
    host = host.rstrip(".")
    labels = host.split(".")
    if "@" in parts.netloc or len(labels) < 2 or not all(_LABEL.match(label) for label in labels) \
            or labels[-1].isdigit() or len(labels[-1]) < 2:
        raise InvalidWebsite(
            f"{str(value).strip()!r} is not a website: expected something like example.com "
            "or https://example.com")
    netloc = host + (f":{port}" if port else "")
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))
