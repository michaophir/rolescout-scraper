"""The normalized role record every source adapter produces."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Role:
    """One open role, normalized across sources. Missing values are ""."""

    source: str             # adapter name: "greenhouse" | "lever" | "ashby"
    source_job_id: str      # the source's own id for the posting
    company: str
    title: str
    department: str
    location: str
    workplace_type: str     # "remote" | "hybrid" | "onsite" | ""
    url: str
    date_posted: str        # ISO date (YYYY-MM-DD) or ""
    compensation_text: str  # free text as published by the source
    description: str        # plain text, full length

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
