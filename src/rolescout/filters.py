"""Cuts of the data: by a company's stage, sectors, HQ, size and VC, and by
where a role is. The same filter narrows the ranked shortlist and a role search."""

import json
import sqlite3
from dataclasses import dataclass, field

from rolescout import profiles, vcs
from rolescout.locations import Requirement, is_remote, split_locations


class FilterError(Exception):
    """A filter that cannot be applied; the message says why."""


def _many(value) -> list[str]:
    """Values given as a list, or as one comma-separated text."""
    if value is None:
        return []
    parts = value if isinstance(value, (list, tuple)) else [value]
    return [p.strip() for part in parts for p in str(part).split(",") if p.strip()]


@dataclass
class RoleFilter:
    stages: list[str] = field(default_factory=list)      # any of these
    sectors: list[str] = field(default_factory=list)     # any of these
    hq: str = ""                                         # text the HQ contains
    location: str = ""                                   # a place one of the role's locations is in
    employees_min: int | None = None
    employees_max: int | None = None
    vc: str = ""                                         # the VC's full name
    vc_id: int | None = None

    def __post_init__(self) -> None:
        self._place = Requirement([self.location]) if self.location and self.location.lower() != "remote" else None

    def __bool__(self) -> bool:
        return bool(self.stages or self.sectors or self.hq or self.location or self.vc
                    or self.employees_min is not None or self.employees_max is not None)

    def describe(self) -> str:
        parts = []
        if self.stages:
            parts.append("stage " + " or ".join(self.stages))
        if self.sectors:
            parts.append("sector " + " or ".join(self.sectors))
        if self.hq:
            parts.append(f"HQ in {self.hq}")
        if self.location:
            parts.append(f"role in {self.location}" if self._place else "remote roles")
        if self.employees_min is not None or self.employees_max is not None:
            low = f"{self.employees_min:,}" if self.employees_min is not None else "0"
            parts.append(f"{low} to {self.employees_max:,} employees" if self.employees_max is not None
                         else f"{low} or more employees")
        if self.vc:
            parts.append(f"linked to {self.vc}")
        return "; ".join(parts)

    def company_ids(self, conn: sqlite3.Connection) -> set[int] | None:
        """The companies that pass the company-level filters; None when there are none to pass.
        A company with no value for a filtered field does not pass."""
        if not (self.stages or self.sectors or self.hq or self.vc_id is not None
                or self.employees_min is not None or self.employees_max is not None):
            return None
        linked = None
        if self.vc_id is not None:
            linked = {r[0] for r in conn.execute("SELECT company_id FROM company_vcs WHERE vc_id = ?", (self.vc_id,))}
        found = set()
        for row in conn.execute(
                "SELECT c.id, p.stage, p.sectors, p.employees, p.hq FROM companies c "
                "LEFT JOIN company_profiles p ON p.company_id = c.id"):
            if linked is not None and row["id"] not in linked:
                continue
            if self.stages and row["stage"] not in self.stages:
                continue
            if self.sectors and not set(self.sectors) & set(json.loads(row["sectors"] or "[]")):
                continue
            if self.hq and self.hq.lower() not in (row["hq"] or "").lower():
                continue
            if self.employees_min is not None or self.employees_max is not None:
                size = row["employees"]
                if size is None or (self.employees_min is not None and size < self.employees_min) \
                        or (self.employees_max is not None and size > self.employees_max):
                    continue
            found.add(row["id"])
        return found

    def role_matches(self, location: str | None, workplace_type: str | None) -> bool:
        """Whether one of the role's locations is in the wanted place. "remote"
        wants a remote role; a place wants that place to be named."""
        if not self.location:
            return True
        places = split_locations(location)
        if self._place is None:
            return (workplace_type or "") == "remote" or any(is_remote(p) for p in places)
        return any(self._place.names(p) for p in places)


def build_filter(conn: sqlite3.Connection, *, stage=None, sector=None, hq: str | None = None,
                 location: str | None = None, employees: str | None = None,
                 employees_min: int | None = None, employees_max: int | None = None,
                 vc: str | None = None) -> RoleFilter:
    """Check the values and make the filter. ``employees`` is a range as text:
    "50-500", "200-" (200 or more) or "-50" (up to 50). Raises ``FilterError``."""
    try:
        stages = list(dict.fromkeys(profiles.parse_stage(s) for s in _many(stage)))
        mapping = profiles.load_mapping(conn)
        sectors = [s for text in _many(sector) for s in mapping.parse(text)]
        owner = vcs.get_vc(conn, vc) if (vc or "").strip() else None
    except (profiles.ProfileError, profiles.MappingError, vcs.VcError) as exc:
        raise FilterError(str(exc)) from None
    if (employees or "").strip():
        low, dash, high = employees.replace(",", "").replace(" ", "").partition("-")
        if not dash or not (low or high) or not (low.isdigit() or not low) or not (high.isdigit() or not high):
            raise FilterError(f"employees must be a range like 50-500, 200- or -50, got {employees!r}")
        employees_min = int(low) if low else employees_min
        employees_max = int(high) if high else employees_max
    if employees_min is not None and employees_max is not None and employees_min > employees_max:
        raise FilterError(f"the employee range is upside down: {employees_min} to {employees_max}")
    return RoleFilter(stages=stages, sectors=list(dict.fromkeys(sectors)), hq=(hq or "").strip(),
                      location=(location or "").strip(), employees_min=employees_min,
                      employees_max=employees_max, vc=owner["name"] if owner else "",
                      vc_id=owner["id"] if owner else None)
