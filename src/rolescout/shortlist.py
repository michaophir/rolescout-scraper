"""The daily shortlist: the open roles to look at first, ranked by a config file.

Ranking reads the local database and one TOML file that lives next to it.
No network, no profile, no model: every point a role gets comes from a rule
in that file, and is reported back in plain words.
"""

import csv
import hashlib
import json
import os
import re
import sqlite3
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rolescout.companies import backed_by
from rolescout.db import transaction, utc_now
from rolescout.locations import Requirement, is_remote, split_locations
from rolescout.pay import PAY_COLUMNS

CONFIG_NAME = "ranking.toml"
CONFIG_ENV_VAR = "ROLESCOUT_RANKING"
KEEP_DAYS = 30
MARKS = ("dismissed", "saved", "applied")
HIDDEN_MARKS = ("dismissed", "applied")     # never shortlisted again
KEEP_DESCRIPTION_MARKS = ("saved", "applied")


class ConfigError(Exception):
    """The ranking config is missing or not usable."""


class MarkError(Exception):
    pass


# --- the config --------------------------------------------------------------

def config_path(db_path: str | os.PathLike, flag: str | os.PathLike | None = None) -> Path:
    """--config flag, then $ROLESCOUT_RANKING, then ranking.toml next to the database."""
    chosen = flag or os.environ.get(CONFIG_ENV_VAR)
    return Path(chosen).expanduser() if chosen else Path(db_path).parent / CONFIG_NAME


def _phrase(phrase: str) -> re.Pattern:
    """Whole words, any case, any spacing: "VP" matches "VP, Product" but not "SVP"."""
    words = [re.escape(w) for w in phrase.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)


@dataclass
class RankingConfig:
    size: int = 100
    max_per_company: int = 5       # slots one company may take; 0 = no cap
    wanted: dict[str, float] = field(default_factory=dict)       # title phrase -> points
    exclude: list[str] = field(default_factory=list)             # title phrases never wanted
    seniority: dict[str, float] = field(default_factory=dict)    # word -> points
    preferred_locations: dict[str, float] = field(default_factory=dict)
    required_locations: list[str] = field(default_factory=list)   # empty = no requirement
    senior_as_staff: list[str] = field(default_factory=list)      # companies whose "Senior" is a "Staff"
    remote_points: float = 0.0
    recency_max_points: float = 0.0
    recency_full_days: int = 0     # first seen this recently: full points
    recency_zero_days: int = 30    # first seen this long ago or more: none
    source: str = ""               # where it was loaded from
    digest: str = ""               # changes whenever the file's content does

    def __post_init__(self) -> None:
        self._wanted = [(p, pts, _phrase(p)) for p, pts in self.wanted.items()]
        self._exclude = [(p, _phrase(p)) for p in self.exclude]
        self._seniority = [(p, pts, _phrase(p)) for p, pts in self.seniority.items()]
        self._locations = [(p, pts, re.compile(re.escape(p), re.I))
                           for p, pts in self.preferred_locations.items()]
        self._required = Requirement(self.required_locations)
        self._leveled = {name.casefold() for name in self.senior_as_staff}
        by_word = {word.casefold(): (word, points) for word, points in self.seniority.items()}
        self._senior, self._staff = by_word.get("senior"), by_word.get("staff")
        self._senior_word = _phrase("Senior")

    def _senior_word_in(self, title: str) -> str:
        """"Senior", as the seniority table or the title spells it."""
        if self._senior:
            return self._senior[0]
        match = self._senior_word.search(title or "")
        return match.group(0) if match else "Senior"

    def leveled_title(self, title: str, company: str | None) -> str | None:
        """What a Senior title at a leveled company is called elsewhere:
        "Senior Product Manager" -> "Staff Product Manager". None when the
        company is not leveled or the title has no "Senior" in it."""
        if not self._staff or (company or "").casefold() not in self._leveled:
            return None
        leveled = self._senior_word.sub(self._staff[0], title or "")
        return leveled if leveled != (title or "") else None

    def wants_title(self, title: str) -> bool:
        """Whether a role with this title can reach the shortlist at all: it
        matches a wanted phrase and no excluded one."""
        title = title or ""
        return (any(pattern.search(title) for _, _, pattern in self._wanted)
                and not any(pattern.search(title) for _, pattern in self._exclude))


def _points_table(raw: object, where: str) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a table of name = points")
    table = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: {key!r} must have a number of points, got {value!r}")
        if not str(key).strip():
            raise ConfigError(f"{where}: empty name")
        table[str(key).strip()] = float(value)
    return table


def _number(section: dict, key: str, default: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key} must be a number, got {value!r}")
    return value


def _only(section: object, allowed: set[str], where: str) -> dict:
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"[{where}] must be a table")
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown setting {', '.join(unknown)} in [{where}]; expected {', '.join(sorted(allowed))}")
    return section


def parse_config(text: str, source: str = "") -> RankingConfig:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source or 'ranking config'} is not valid TOML: {exc}") from None
    raw = _only(raw, {"size", "max_per_company", "tier_points", "title", "seniority", "leveling",
                      "location", "recency"},
                "top level")
    leveled = _only(raw.get("leveling"), {"senior_as_staff"}, "leveling").get("senior_as_staff", [])
    if not isinstance(leveled, list) or not all(isinstance(x, str) and x.strip() for x in leveled):
        raise ConfigError('leveling.senior_as_staff must be a list of company names, e.g. ["Acme"]')
    title = _only(raw.get("title"), {"wanted", "exclude"}, "title")
    location = _only(raw.get("location"), {"preferred", "remote_points", "require"}, "location")
    required = location.get("require", [])
    if not isinstance(required, list) or not all(isinstance(x, str) and x.strip() for x in required):
        raise ConfigError('location.require must be a list of places, e.g. ["United States"]')
    recency = _only(raw.get("recency"), {"max_points", "full_days", "zero_days"}, "recency")

    size = raw.get("size", 100)
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ConfigError(f"size must be a whole number of 1 or more, got {size!r}")
    cap = raw.get("max_per_company", 5)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise ConfigError(f"max_per_company must be a whole number, 0 for no cap, got {cap!r}")
    exclude = title.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(x, str) and x.strip() for x in exclude):
        raise ConfigError("title.exclude must be a list of phrases")
    # [tier_points] is still accepted so an older config loads, but a
    # company's tier no longer earns points.
    full, zero = (int(_number(recency, "full_days", 0, "recency")),
                  int(_number(recency, "zero_days", 30, "recency")))
    if zero <= full:
        raise ConfigError("recency.zero_days must be larger than recency.full_days")
    seniority = _points_table(_only(raw.get("seniority"), {"words"}, "seniority").get("words"),
                              "[seniority.words]")
    if leveled and not {"senior", "staff"} <= {word.casefold() for word in seniority}:
        raise ConfigError('leveling.senior_as_staff needs both "Senior" and "Staff" in [seniority.words]: '
                          "it gives a Senior title the points of Staff")
    return RankingConfig(
        size=size, max_per_company=cap,
        wanted=_points_table(title.get("wanted"), "[title.wanted]"),
        exclude=[x.strip() for x in exclude],
        seniority=seniority, senior_as_staff=[x.strip() for x in leveled],
        preferred_locations=_points_table(location.get("preferred"), "[location.preferred]"),
        required_locations=[x.strip() for x in required],
        remote_points=float(_number(location, "remote_points", 0, "location")),
        recency_max_points=float(_number(recency, "max_points", 0, "recency")),
        recency_full_days=full, recency_zero_days=zero,
        source=source, digest=hashlib.sha256(text.encode()).hexdigest()[:12])


def load_config(path: str | os.PathLike) -> RankingConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"no ranking config at {path}. Create one with: rolescout shortlist init")
    return parse_config(path.read_text(encoding="utf-8"), str(path))


def read_filters_csv(path: str | os.PathLike) -> tuple[list[str], list[str]]:
    """(title phrases, seniority words) from a role_filters.csv-style file (field,value)."""
    titles, seniority = [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            kind, value = (row.get("field") or "").strip().lower(), (row.get("value") or "").strip()
            if value and kind == "title":
                titles.append(value)
            elif value and kind == "seniority":
                seniority.append(value)
    return list(dict.fromkeys(titles)), list(dict.fromkeys(seniority))


# The starter ranking: a generic starting point for product and engineering
# roles. Every title and every seniority word earns the same points, so the
# file states no preference until its owner edits it.
STARTER_TITLES = [
    "Product Manager", "Product Lead", "Product Owner", "Group Product Manager", "Technical Product Manager",
    "Head of Product", "Director of Product", "VP of Product",
    "Software Engineer", "Software Developer", "Backend Engineer", "Frontend Engineer", "Full Stack Engineer",
    "Mobile Engineer", "Machine Learning Engineer", "Data Engineer", "Platform Engineer",
    "Infrastructure Engineer", "Site Reliability Engineer", "Security Engineer", "Staff Engineer",
    "Principal Engineer", "Member of Technical Staff", "Engineering Manager", "Director of Engineering",
    "Head of Engineering", "VP of Engineering",
]
STARTER_SENIORITY = ["Senior", "Staff", "Principal", "Lead", "Group", "Director", "Head", "VP"]
STARTER_EXCLUDE = ["Intern", "Internship", "New Grad", "New Graduate", "University Graduate", "Early Career",
                   "Apprentice", "Co-op", "Working Student"]


def starter_config(titles: list[str] | None = None, seniority: list[str] | None = None,
                   seeded_from: str = "") -> str:
    """The text of a starter ranking.toml."""
    def table(names: list[str], points: int) -> str:
        return "\n".join(f"{json.dumps(name)} = {points}" for name in names)
    example = "a starting point for product and engineering roles: make it yours"
    origin = f"seeded from {seeded_from}" if seeded_from and titles else example
    seniority_origin = f"seeded from {seeded_from}" if seeded_from and seniority else example
    exclude = ", ".join(json.dumps(word) for word in STARTER_EXCLUDE)
    return f"""\
# rolescout ranking: which open roles reach your shortlist, and in what order.
#
# This file is yours: edit it freely, then run `rolescout shortlist rebuild`.
# Rebuilding reads only the local database; it makes no network call.
# It is personal data. Keep it next to the database, not in the repository.
#
# How a role is scored: it gets points from each section below and the points
# are added up. Higher scores rank first. `rolescout why ID` shows the sum
# for any role.

# How many roles the shortlist holds.
size = 100

# The most slots one company may take, so a company with many matching roles
# cannot crowd out the rest: its best-scoring roles get the slots and the
# others are held back. Roles you marked saved are never held back.
# 0 means no cap.
max_per_company = 5

[title]
# A role whose title contains any of these phrases is never listed.
# Whole words, any capitalization. Add your own, e.g. "Contract" or "Manager".
exclude = [{exclude}]

# Title phrases you want, with points ({origin}).
# A role must match at least one of these or it is never listed.
# Whole words, any capitalization: "Product Manager" matches "Senior Product
# Manager, Growth" but not "Product Marketing Manager". If several match, the
# one worth the most points counts. Raise the points of the titles you want most.
[title.wanted]
{table(titles or STARTER_TITLES, 30)}

# Seniority words in the title, with points ({seniority_origin}).
# If several match, the one worth the most counts.
[seniority.words]
{table(seniority or STARTER_SENIORITY, 10)}

# Leveling: at the companies named here a "Senior" title is what most others
# call "Staff", so it is scored as the Staff title would be: "Senior Product
# Manager" earns the title points of "Staff Product Manager" (when that is a
# wanted phrase worth more) and the seniority points of "Staff".
# Use the names as `rolescout companies list` shows them (capitals do not
# matter), e.g. senior_as_staff = ["Acme", "Globex"]. `rolescout why ID` says
# when this applied.
[leveling]
senior_as_staff = []

[location]
# Points when the role is remote (the board says so, or the location does).
# 0 means remote roles get no preference. Example: remote_points = 10
remote_points = 0

# Location requirement, off unless you remove the "#" below. With it, a role
# reaches the shortlist only if at least one of its locations is in one of
# these places. "United States" covers every form boards use: United States,
# USA, US, each state and its two-letter code, and major US cities. Any other
# entry is matched as written, e.g. "London". A remote role that names no
# country or city counts as a match; "Remote - Canada" does not. A role with
# no location at all does not match. `rolescout why ID` says when a role was
# kept off the list by this rule.
# require = ["United States"]

# Points when the location contains one of these. None by default; add a
# line per place under the heading below, e.g.
#   "New York" = 10
#   "London" = 5
# Any capitalization. A role that is both remote and in a preferred place
# gets the larger of the two, not both.
[location.preferred]

# Newer roles first. A role's age is counted from the date the company posted
# it. When the board gives no posted date, the date rolescout first saw it is
# used instead, except for roles already there when a company was first
# fetched: their age is unknown, so they get no points here.
# A role up to `full_days` old gets `max_points`, fading evenly to nothing
# by `zero_days`.
[recency]
max_points = 15
full_days = 3
zero_days = 30
"""


# --- scoring -----------------------------------------------------------------

@dataclass
class LocationRead:
    places: list[str]                 # the separate locations, as given
    preferred: tuple[str, float] | None = None   # (phrase, points) of the best preferred match
    preferred_place: str | None = None
    remote: bool = False
    remote_place: str | None = None
    required_place: str | None = None  # the location that satisfied the requirement
    meets_requirement: bool = True


def read_location(role, cfg: RankingConfig) -> LocationRead:
    """Split a role's location text and work out which place matters."""
    places = split_locations(role["location"])
    out = LocationRead(places=places)
    out.preferred = _best([(p, pts) for p, pts, pattern in cfg._locations
                           if any(pattern.search(place) for place in places)])
    if out.preferred:
        pattern = next(pat for p, _, pat in cfg._locations if p == out.preferred[0])
        out.preferred_place = next(place for place in places if pattern.search(place))
    out.remote_place = next((place for place in places if is_remote(place)), None)
    out.remote = role["workplace_type"] == "remote" or out.remote_place is not None
    if cfg._required:
        out.required_place = cfg._required.first_match(places)
        # Marked remote by the board with no place given at all: nothing rules it out.
        out.meets_requirement = out.required_place is not None or (not places and out.remote)
    return out


def display_location(role, cfg: RankingConfig | None, also: list[str] | tuple = ()) -> str:
    """The location to show in a table: the place that earned the location
    points (or met the requirement) first, then how many more there are.
    ``also`` holds the places of the same job's other postings; they count
    among the "more"."""
    places = split_locations(role["location"])
    everywhere = list(dict.fromkeys([*places, *also]))
    more = f" +{len(everywhere) - 1} more" if len(everywhere) > 1 else ""
    if len(places) <= 1 or cfg is None:
        return (places[0] if places else (role["location"] or (everywhere[0] if everywhere else ""))) + more
    read = read_location(role, cfg)
    place_points = read.preferred[1] if read.preferred else 0.0
    remote_points = cfg.remote_points if read.remote else 0.0
    if remote_points > place_points and read.remote_place:
        # Remote earned the points. With a requirement, lead with a remote place
        # that meets it ("Remote - US"), not one that does not ("Remote - Canada").
        fits = [p for p in places if is_remote(p) and (not cfg._required or cfg._required.accepts(p))]
        lead = fits[0] if fits else (read.required_place or read.remote_place)
    elif read.preferred_place:
        lead = read.preferred_place
    else:
        lead = read.required_place or read.remote_place or places[0]
    return lead + more


@dataclass
class Evaluation:
    eligible: bool
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)   # what earned points
    detail: list[str] = field(default_factory=list)    # every rule, including the ones that gave nothing
    why_not: str = ""                                  # when not eligible


def _pts(points: float) -> str:
    return f"+{points:g}"


def _best(matches: list[tuple[str, float]]) -> tuple[str, float] | None:
    # Most points wins; between equals, the longer (more specific) phrase.
    return max(matches, key=lambda m: (m[1], len(m[0]))) if matches else None


def _days_since(stamp: str, today: date) -> int:
    return max(0, (today - date.fromisoformat(stamp[:10])).days)


def role_age(role, today: date, baseline: bool = False) -> tuple[int | None, str]:
    """(age in days or None if unknown, which date it was counted from).

    The board's posted date when there is a usable one, else the day the role
    was first seen. A role first seen in its board's baseline fetch was already
    open before we looked, so without a posted date its age is unknown.
    """
    posted = (role["date_posted"] or "")[:10]
    try:
        if posted and date.fromisoformat(posted) <= today + timedelta(days=1):
            return _days_since(posted, today), "posted"
    except ValueError:
        pass
    if baseline:
        return None, "baseline"
    return _days_since(role["first_seen_at"], today), "first_seen"


def evaluate(role, cfg: RankingConfig, today: date, baseline: bool = False) -> Evaluation:
    """Score one role. ``role`` needs title, location, workplace_type, date_posted,
    first_seen_at. ``baseline`` says the role was first seen in its
    board's baseline fetch."""
    title = role["title"] or ""
    ev = Evaluation(eligible=True)

    def add(points: float, earned: str, nothing: str) -> None:
        if points:
            ev.score += points
            ev.reasons.append(f"{earned} ({_pts(points)})")
            ev.detail.append(f"{earned} ({_pts(points)})")
        else:
            ev.detail.append(f"{nothing} (+0)")

    def best_title(text: str):
        return _best([(p, pts) for p, pts, pattern in cfg._wanted if pattern.search(text)])

    def best_seniority(text: str):
        return _best([(p, pts) for p, pts, pattern in cfg._seniority if pattern.search(text)])

    company = role["company"] if "company" in role.keys() else ""
    # Leveling: at the listed companies a Senior title is another company's
    # Staff, so it is scored as that title would be, for the title phrase and
    # for the seniority word alike. It never scores lower than as written.
    as_staff = cfg.leveled_title(title, company)
    excluded = next((p for p, pattern in cfg._exclude if pattern.search(title)), None)
    wanted, leveled_wanted = best_title(title), best_title(as_staff) if as_staff else None
    title_leveled = leveled_wanted is not None and (wanted is None or leveled_wanted[1] > wanted[1])
    if title_leveled:
        wanted = leveled_wanted
    if excluded:
        ev.eligible, ev.why_not = False, f'its title contains "{excluded}", a phrase you never want'
    elif wanted is None:
        ev.eligible, ev.why_not = False, "its title matches none of the title phrases you want"

    if wanted:
        ev.score += wanted[1]
        line = (f'Title counts as "{as_staff}" at {company} (leveling), which matches "{wanted[0]}" '
                f"({_pts(wanted[1])})" if title_leveled else f'Title matches "{wanted[0]}" ({_pts(wanted[1])})')
        ev.reasons.append(line)
        ev.detail.append(line)
    else:
        ev.detail.append("Title matches no wanted phrase (+0)")
    senior, leveled_senior = best_seniority(title), best_seniority(as_staff) if as_staff else None
    if leveled_senior is not None and (senior is None or leveled_senior[1] > senior[1]):
        add(leveled_senior[1], f'Seniority: "{cfg._senior_word_in(title)}" counts as "{leveled_senior[0]}" '
                               f"at {company} (leveling)", "")
    else:
        add(senior[1] if senior else 0.0, f'Seniority: "{senior[0]}"' if senior else "",
            "No seniority word in the title")

    where = read_location(role, cfg)
    place, remote = where.preferred, where.remote
    if cfg._required:
        if where.meets_requirement:
            ev.detail.append(f"Location requirement met by {where.required_place or 'a remote role with no place given'}")
        else:
            given = "; ".join(where.places) if where.places else "no location given"
            ev.detail.append(f"Location requirement not met ({given})")
            if ev.eligible:
                ev.eligible = False
                ev.why_not = (f"none of its locations ({given}) is in the places you require "
                              f"({cfg._required.describe()})" if where.places else
                              f"it gives no location, and you require one in {cfg._required.describe()}")
    place_points = place[1] if place else 0.0
    remote_points = cfg.remote_points if remote else 0.0
    if remote_points > place_points:
        add(remote_points, "Remote", "")
    elif place_points:
        add(place_points, f'Location matches "{place[0]}"', "")
    else:
        add(0.0, "", "Remote, but remote earns no points" if remote
            else "Not remote and not in a preferred location")

    days, counted_from = role_age(role, today, baseline)
    if days is None:
        ev.detail.append("No posted date, and it was already open when the company was first "
                         "fetched, so its age is unknown (+0)")
    else:
        if days <= cfg.recency_full_days:
            share = 1.0
        elif days >= cfg.recency_zero_days:
            share = 0.0
        else:
            share = (cfg.recency_zero_days - days) / (cfg.recency_zero_days - cfg.recency_full_days)
        when = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
        label = f"Posted {when}" if counted_from == "posted" else f"First seen {when} (no posted date)"
        add(round(cfg.recency_max_points * share, 1), label, label)
    ev.score = round(ev.score, 1)
    return ev


# --- building and reading lists ----------------------------------------------

def baseline_fetches(conn: sqlite3.Connection) -> dict[int, set[str]]:
    """For each board, the start time of its baseline run(s): a role whose
    first-seen time is one of these was already open when we first looked."""
    found: dict[int, set[str]] = {}
    for row in conn.execute(
            "SELECT b.id, ru.started_at FROM boards b JOIN run_boards rb ON rb.source = b.source "
            "AND rb.ref = b.ref AND rb.baseline = 1 JOIN runs ru ON ru.id = rb.run_id"):
        found.setdefault(row["id"], set()).add(row["started_at"])
    return found


def in_baseline(role, baselines: dict[int, set[str]]) -> bool:
    return role["first_seen_at"] in baselines.get(role["board_id"], ())


ROLE_SELECT = (
    "SELECT r.id, r.board_id, r.company_id, r.title, r.department, r.location, r.workplace_type, r.url, r.date_posted, "
    "r.compensation_text, r.pay_min, r.pay_max, r.pay_currency, r.pay_period, r.pay_varies, r.pay_source, "
    "r.first_seen_at, r.closed_at, c.name AS company, c.active, "
    "m.status AS mark, m.note AS mark_note "
    "FROM roles r JOIN companies c ON c.id = r.company_id "
    "LEFT JOIN role_marks m ON m.role_id = r.id ")


def job_key(role) -> tuple[int, str]:
    """The company and the title, whatever the capitals and the spacing.
    Postings that differ in it are never one job; postings that share it are
    one job only when ``SameJob`` says so."""
    return role["company_id"], " ".join((role["title"] or "").split()).casefold()


class SameJob:
    """Decides whether two postings are clearly one job posted more than once.

    The same company and title is not enough: a large company posts many
    different jobs under one title. Two postings are one job when they share
    company and title and

    - both have a stored description, and the two texts are identical once
      whitespace and the places the postings are for are taken out; or
    - one of them has no stored description, and they have the same posted
      date and the same department, both known.

    Anything else is two jobs: when in doubt, postings stay apart.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._titles: dict[int, dict[int, str]] = {}      # company -> open role -> title key
        # company and title -> open role -> (description digest or "", posted date, department)
        self._found: dict[tuple[int, str], dict[int, tuple[str, str, str]]] = {}
        self._hidden: dict[tuple[int, str], list[sqlite3.Row]] | None = None

    def _signatures(self, key: tuple[int, str]) -> dict[int, tuple[str, str, str]]:
        """For every open posting with this company and title: a digest of
        its description as compared, its posted date and its department."""
        if key in self._found:
            return self._found[key]
        company_id = key[0]
        if company_id not in self._titles:
            self._titles[company_id] = {
                r["id"]: job_key(r)[1] for r in self._conn.execute(
                    "SELECT id, company_id, title FROM roles WHERE company_id = ? AND closed_at IS NULL", (company_id,))}
        ids = [role_id for role_id, title in self._titles[company_id].items() if title == key[1]]
        rows = self._conn.execute(
            "SELECT r.id, r.location, r.department, r.date_posted, d.description FROM roles r "
            f"LEFT JOIN role_descriptions d ON d.role_id = r.id WHERE r.id IN ({', '.join('?' * len(ids))})",
            ids).fetchall()
        # What names a place in any of these postings: the location texts,
        # each place in them, and the parts of a place ("Austin", "TX").
        # Taken out of every description alike, so texts that differ only in
        # where the job is, or that each list every city, compare equal.
        places: set[str] = set()
        for row in rows:
            for place in (row["location"] or "", *split_locations(row["location"])):
                places.update(part.strip() for part in (place, *place.split(",")))
        pattern = None
        if any(len(p) > 1 for p in places):
            pattern = re.compile("|".join(
                rf"(?<!\w){re.escape(p)}(?!\w)" for p in sorted(places, key=len, reverse=True) if len(p) > 1), re.I)
        found = {}
        for row in rows:
            text = row["description"] or ""
            if pattern is not None:
                text = pattern.sub(" ", text)
            text = "".join(text.split()).casefold()
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
            found[row["id"]] = (digest, (row["date_posted"] or "")[:10], " ".join((row["department"] or "").split()).casefold())
        self._found[key] = found
        return found

    def __call__(self, a, b) -> bool:
        key = job_key(a)
        if key != job_key(b):
            return False
        if a["id"] == b["id"]:
            return True
        signatures = self._signatures(key)
        one, other = signatures.get(a["id"]), signatures.get(b["id"])
        if one is None or other is None:
            return False                      # a closed posting: nothing to compare it by
        if one[0] and other[0]:
            return one[0] == other[0]
        return bool(one[1] and one[2]) and one[1:] == other[1:]

    def covering_mark(self, role) -> sqlite3.Row | None:
        """The open posting marked dismissed or applied that is the same job
        as ``role``, if there is one. The mark covers the job: the postings
        folded with it stay off the list too. A posting that merely shares
        its title is not covered."""
        if self._hidden is None:
            self._hidden = {}
            for row in self._conn.execute(
                    ROLE_SELECT + "WHERE r.closed_at IS NULL AND m.status IN ('dismissed', 'applied') ORDER BY r.id"):
                self._hidden.setdefault(job_key(row), []).append(row)
        return next((marked for marked in self._hidden.get(job_key(role), ())
                     if marked["id"] != role["id"] and self(role, marked)), None)


def rank_roles(conn: sqlite3.Connection, cfg: RankingConfig, now: datetime | None = None,
               company_id: int | None = None, qualifying_only: bool = True) -> list[tuple]:
    """Every open posting that qualifies, best first, as (role, evaluation).

    Left out: closed roles, roles of deactivated companies, roles marked
    dismissed or applied and the other postings of the same job (as
    ``SameJob`` tells it), and (unless ``qualifying_only`` is False) roles
    that fail the title or location rules. No size cut and no per-company cap.

    Order: qualifying roles first, then the higher score. Roles with the
    same score are ordered by posted date, newest first (the day a role was
    first seen stands in where the board gives no date; a role with neither
    comes last), then by company name, then by title and ID so that the
    order is the same every time.
    """
    now = now or datetime.now().astimezone()
    today = now.astimezone(timezone.utc).date()
    baselines = baseline_fetches(conn)
    same = SameJob(conn)
    ranked = []
    for role in conn.execute(
            ROLE_SELECT + "WHERE r.closed_at IS NULL AND c.active = 1 "
            "AND (m.status IS NULL OR m.status NOT IN ('dismissed', 'applied')) "
            "AND (? IS NULL OR r.company_id = ?)", (company_id, company_id)):
        if same.covering_mark(role) is not None:
            continue
        baseline = in_baseline(role, baselines)
        ev = evaluate(role, cfg, today, baseline)
        if ev.eligible or not qualifying_only:
            age = role_age(role, today, baseline)[0]
            ranked.append((role, ev, age if age is not None else 10 ** 6))
    ranked.sort(key=lambda item: (
        not item[1].eligible, -item[1].score, item[2],
        item[0]["company"].casefold(), item[0]["title"].casefold(), item[0]["id"]))
    return [(role, ev) for role, ev, _ in ranked]


@dataclass
class Job:
    """One job as the lists show it: its best-scoring posting, and the same
    job's other postings (other cities, usually), best first."""

    role: sqlite3.Row
    ev: Evaluation
    others: list[tuple] = field(default_factory=list)    # (role, evaluation)

    @property
    def postings(self) -> list:
        return [self.role, *(role for role, _ in self.others)]


def fold_jobs(ranked: list[tuple], same: SameJob, keep=None) -> list[Job]:
    """What ``rank_roles`` gives, with the postings of one job folded into
    one entry.

    A posting joins a job when ``same`` says it is the same job as every
    posting already in it; otherwise it is an entry of its own. A job takes
    its score and its place from its best-scoring posting, which is the one
    that comes first in ``ranked``. ``keep`` (a function of a role) drops
    postings before they are folded, so a job filtered by location is led
    by, and lists, only the postings that pass.
    """
    jobs: list[Job] = []
    by_title: dict[tuple[int, str], list[Job]] = {}
    for role, ev in ranked:
        if keep is not None and not keep(role):
            continue
        candidates = by_title.setdefault(job_key(role), [])
        job = next((j for j in candidates if all(same(role, posting) for posting in j.postings)), None)
        if job is None:
            job = Job(role, ev)
            candidates.append(job)
            jobs.append(job)
        else:
            job.others.append((role, ev))
    return jobs


def _other_places(lead, others) -> list[str]:
    """The places of a job's other postings that its lead posting does not name."""
    mine = set(split_locations(lead["location"]))
    return [place for place in dict.fromkeys(p for other in others for p in split_locations(other["location"]))
            if place not in mine]


def _as_one_entry(entry: dict, lead, others: list[dict], cfg: RankingConfig | None) -> dict:
    """Fill in what an entry says about the job's other postings: every
    location in one text (the lead posting's first), how many postings there
    are, and each other posting with its ID, location, URL and mark."""
    also = _other_places(lead, others)
    entry["location"] = "; ".join(part for part in (lead["location"], *also) if part)
    entry["location_shown"] = display_location(lead, cfg, also)
    entry["postings"] = 1 + len(others)
    entry["other_postings"] = others
    if entry.get("mark") is None and any(other["mark"] == "saved" for other in others):
        entry["mark"] = "saved"       # saved under another city's posting: the job is saved
    return entry


def _other(role, score: float) -> dict:
    return {"id": role["id"], "location": role["location"], "url": role["url"], "score": score, "mark": role["mark"]}


def filtered_entries(conn: sqlite3.Connection, cfg: RankingConfig, only, now: datetime | None = None) -> dict:
    """Rank, right now, every qualifying job that passes a filter
    (``rolescout.filters.RoleFilter``): not a cut of today's stored list, so
    a job that never made the list can lead here. No size cut, no
    per-company cap. Each entry has the shape ``shortlist_entries`` gives,
    plus ``list_rank``: its rank on today's stored list, or None.

    A job posted once per location (``SameJob``) is one entry. A location filter picks
    among its postings first, so the entry is led by, and lists, the
    postings that pass.
    """
    companies = only.company_ids(conn)
    ranked = rank_roles(conn, cfg, now)
    same = SameJob(conn)
    jobs = fold_jobs(ranked, same, keep=lambda role: (
        (companies is None or role["company_id"] in companies)
        and only.role_matches(role["location"], role["workplace_type"])))
    current = latest_shortlist(conn)
    on_list = _covered(conn, current["id"]) if current else {}
    backers = backed_by(conn)
    entries = []
    for job in jobs:
        role = job.role
        entry = {
            "rank": len(entries) + 1, "score": job.ev.score, "reasons": job.ev.reasons,
            **{k: role[k] for k in ("id", "title", "department", "location", "workplace_type", "url",
                                    "date_posted", *PAY_COLUMNS, "first_seen_at", "closed_at",
                                    "company_id", "company", "mark", "mark_note")},
            "backed_by": backers.get(role["company_id"], []),
            "list_rank": next((on_list[r["id"]] for r in job.postings if r["id"] in on_list), None)}
        entries.append(_as_one_entry(entry, role, [_other(r, ev.score) for r, ev in job.others], cfg))
    return {"entries": entries, "qualifying": len(fold_jobs(ranked, same))}


def build_shortlist(conn: sqlite3.Connection, cfg: RankingConfig, *, run_id: int | None = None,
                    now: datetime | None = None) -> dict:
    """Rank every open role and store the top ``cfg.size`` as today's shortlist.

    Left out: closed roles, roles of deactivated companies, roles marked
    dismissed or applied, and roles that fail the title rules. A job posted
    once per location (``SameJob``) is one entry, its best-scoring posting,
    and takes one slot. No company takes more than ``cfg.max_per_company`` slots; jobs
    that scored high enough but lost out to that cap are recorded as held
    back. A list built earlier the same local day is replaced; lists older
    than 30 days are dropped.
    """
    now = now or datetime.now().astimezone()
    list_date = now.date().isoformat()
    jobs = fold_jobs(rank_roles(conn, cfg, now), SameJob(conn))

    top, held, taken = [], [], {}
    for job in jobs:
        if len(top) >= cfg.size:
            break
        company = job.role["company"]
        # A saved job is never held back, but it does use one of the slots.
        if cfg.max_per_company and taken.get(company, 0) >= cfg.max_per_company \
                and not any(role["mark"] == "saved" for role in job.postings):
            held.append(job)
            continue
        taken[company] = taken.get(company, 0) + 1
        top.append(job)

    cutoff = (now.date() - timedelta(days=KEEP_DAYS)).isoformat()
    with transaction(conn):
        conn.execute("DELETE FROM shortlists WHERE list_date = ? OR list_date < ?", (list_date, cutoff))
        shortlist_id = conn.execute(
            "INSERT INTO shortlists (list_date, built_at, run_id, config_hash, eligible) "
            "VALUES (?, ?, ?, ?, ?)",
            (list_date, now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), run_id,
             cfg.digest, len(jobs))).lastrowid
        conn.executemany(
            "INSERT INTO shortlist_entries (shortlist_id, rank, role_id, score, reasons) "
            "VALUES (?, ?, ?, ?, ?)",
            [(shortlist_id, rank, job.role["id"], job.ev.score, json.dumps(job.ev.reasons, ensure_ascii=False))
             for rank, job in enumerate(top, start=1)])
        # In ranked order, which is the order they are read back in.
        conn.executemany(
            "INSERT INTO shortlist_held (shortlist_id, role_id, score) VALUES (?, ?, ?)",
            [(shortlist_id, job.role["id"], job.ev.score) for job in held])
        conn.executemany(
            "INSERT INTO shortlist_postings (shortlist_id, role_id, lead_role_id, score) VALUES (?, ?, ?, ?)",
            [(shortlist_id, role["id"], job.role["id"], ev.score)
             for job in (*top, *held) for role, ev in job.others])
    previous, new_ids = new_since(conn, shortlist_id)
    return {"shortlist_id": shortlist_id, "list_date": list_date, "listed": len(top),
            "eligible": len(jobs), "held_back": len(held),
            "new": len(new_ids) if previous else None,
            "previous_date": previous["list_date"] if previous else None}


def latest_shortlist(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM shortlists ORDER BY list_date DESC LIMIT 1").fetchone()


def _covered(conn: sqlite3.Connection, shortlist_id: int) -> dict[int, int]:
    """Every posting a list shows, with the rank of its entry: the entries
    themselves and the other postings of their jobs."""
    rows = conn.execute(
        "SELECT e.role_id, e.rank FROM shortlist_entries e WHERE e.shortlist_id = :id "
        "UNION ALL SELECT p.role_id, e.rank FROM shortlist_postings p JOIN shortlist_entries e "
        "ON e.shortlist_id = p.shortlist_id AND e.role_id = p.lead_role_id WHERE p.shortlist_id = :id",
        {"id": shortlist_id})
    return {r["role_id"]: r["rank"] for r in rows}


def new_between(conn: sqlite3.Connection, shortlist_id: int, earlier_id: int) -> set[int]:
    """The entries of a list (by role ID) that an earlier list did not have.
    An entry is not new when the earlier list showed any posting of its job:
    the same job led by another city's posting is the same entry."""
    before = _covered(conn, earlier_id)
    entries: dict[int, list[int]] = {}
    for role_id, rank in _covered(conn, shortlist_id).items():
        entries.setdefault(rank, []).append(role_id)
    leads = {r["rank"]: r["role_id"] for r in conn.execute(
        "SELECT rank, role_id FROM shortlist_entries WHERE shortlist_id = ?", (shortlist_id,))}
    return {leads[rank] for rank, role_ids in entries.items() if not any(i in before for i in role_ids)}


def new_since(conn: sqlite3.Connection, shortlist_id: int, since: str | None = None):
    """(the list being compared against or None, ids of roles on this list but not on that one).

    By default the comparison is with the previous day's list. ``since``
    (YYYY-MM-DD) picks the list of that day, or the nearest earlier one.
    """
    current = conn.execute("SELECT * FROM shortlists WHERE id = ?", (shortlist_id,)).fetchone()
    bound = min(date.fromisoformat(since).isoformat(), current["list_date"]) if since else None
    previous = conn.execute(
        "SELECT * FROM shortlists WHERE list_date < ? AND (? IS NULL OR list_date <= ?) "
        "ORDER BY list_date DESC LIMIT 1", (current["list_date"], bound, bound)).fetchone()
    if previous is None:
        return None, set()
    return previous, new_between(conn, shortlist_id, previous["id"])


def _other_postings(conn: sqlite3.Connection, shortlist_id: int) -> dict[int, list[dict]]:
    """For each entry or held-back role of a list, the other postings of its
    job, best first."""
    found: dict[int, list[dict]] = {}
    for row in conn.execute(
            "SELECT p.lead_role_id, p.score, r.id, r.location, r.url, m.status AS mark "
            "FROM shortlist_postings p JOIN roles r ON r.id = p.role_id "
            "LEFT JOIN role_marks m ON m.role_id = r.id WHERE p.shortlist_id = ? ORDER BY p.rowid", (shortlist_id,)):
        found.setdefault(row["lead_role_id"], []).append(_other(row, row["score"]))
    return found


def shortlist_entries(conn: sqlite3.Connection, shortlist_id: int,
                      cfg: RankingConfig | None = None) -> list[dict]:
    """The entries of a list, in rank order. An entry is one job: its
    best-scoring posting, with ``location`` naming every place the job is
    posted for, ``postings`` saying how many postings that is and
    ``other_postings`` listing the rest. ``location_shown`` leads with the
    place that mattered when ``cfg`` is given; ``backed_by`` names the
    portfolio boards that list the company."""
    backers = backed_by(conn)
    others = _other_postings(conn, shortlist_id)
    rows = conn.execute(
        "SELECT e.rank, e.score, e.reasons, r.id, r.title, r.department, r.location, "
        "r.workplace_type, r.url, r.date_posted, r.compensation_text, r.pay_min, r.pay_max, r.pay_currency, r.pay_period, r.pay_varies, r.pay_source, "
        "r.first_seen_at, r.closed_at, "
        "c.id AS company_id, c.name AS company, m.status AS mark, m.note AS mark_note "
        "FROM shortlist_entries e JOIN roles r ON r.id = e.role_id "
        "JOIN companies c ON c.id = r.company_id LEFT JOIN role_marks m ON m.role_id = r.id "
        "WHERE e.shortlist_id = ? ORDER BY e.rank", (shortlist_id,))
    return [_as_one_entry({**dict(r), "reasons": json.loads(r["reasons"]),
                           "backed_by": backers.get(r["company_id"], [])}, r, others.get(r["id"], []), cfg)
            for r in rows]


def held_back(conn: sqlite3.Connection, shortlist_id: int) -> list[dict]:
    """Jobs kept off this list by the per-company cap, in the order they
    ranked: best score first, ties as ``rank_roles`` breaks them."""
    others = _other_postings(conn, shortlist_id)
    rows = conn.execute(
        "SELECT h.score, r.id, r.title, r.location, r.url, c.name AS company "
        "FROM shortlist_held h JOIN roles r ON r.id = h.role_id "
        "JOIN companies c ON c.id = r.company_id WHERE h.shortlist_id = ? "
        "ORDER BY h.rowid", (shortlist_id,))
    return [{**dict(r), "postings": 1 + len(others.get(r["id"], []))} for r in rows]


# --- marks -------------------------------------------------------------------

def parse_role_id(text: str) -> int:
    cleaned = str(text).strip().lstrip("#")
    if not cleaned.isdigit():
        raise MarkError(f"{text!r} is not a role ID; IDs are the numbers in the ID column")
    return int(cleaned)


def get_role(conn: sqlite3.Connection, role_id: int) -> sqlite3.Row:
    row = conn.execute(ROLE_SELECT + "WHERE r.id = ?", (role_id,)).fetchone()
    if row is None:
        raise MarkError(f"no role with ID {role_id}")
    return row


def _drop_description_if_closed(conn: sqlite3.Connection, role_id: int) -> None:
    conn.execute(
        "DELETE FROM role_descriptions WHERE role_id = ? AND role_id IN "
        "(SELECT id FROM roles WHERE closed_at IS NOT NULL) AND role_id NOT IN "
        "(SELECT role_id FROM role_marks WHERE status IN ('saved', 'applied'))", (role_id,))


def mark_role(conn: sqlite3.Connection, role_id: int, status: str, note: str = "") -> sqlite3.Row:
    """Mark a role dismissed, saved or applied. A new mark replaces the old one."""
    if status not in MARKS:
        raise MarkError(f"status must be one of {', '.join(MARKS)}, got {status!r}")
    get_role(conn, role_id)
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO role_marks (role_id, status, note, marked_at) VALUES (?, ?, ?, ?)",
            (role_id, status, note.strip(), utc_now()))
        _drop_description_if_closed(conn, role_id)  # e.g. saved -> dismissed on a closed role
    return get_role(conn, role_id)


def other_open_postings(conn: sqlite3.Connection, role) -> list[sqlite3.Row]:
    """The open postings that are the same job as ``role`` (``SameJob``),
    other than itself."""
    same = SameJob(conn)
    rows = conn.execute(ROLE_SELECT + "WHERE r.company_id = ? AND r.closed_at IS NULL AND r.id != ? ORDER BY r.id",
                        (role["company_id"], role["id"]))
    return [row for row in rows if same(row, role)]


def unmark_role(conn: sqlite3.Connection, role_id: int) -> str | None:
    """Remove a role's mark. Returns the status it had, or None if it had none."""
    role = get_role(conn, role_id)
    with transaction(conn):
        conn.execute("DELETE FROM role_marks WHERE role_id = ?", (role_id,))
        _drop_description_if_closed(conn, role_id)
    return role["mark"]


def list_marks(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    rows = conn.execute(
        "SELECT r.id, m.status, m.note, m.marked_at, c.name AS company, r.title, r.location, r.url, "
        "r.closed_at, EXISTS (SELECT 1 FROM role_descriptions d WHERE d.role_id = r.id) AS has_description "
        "FROM role_marks m JOIN roles r ON r.id = m.role_id JOIN companies c ON c.id = r.company_id "
        "WHERE ? IS NULL OR m.status = ? ORDER BY m.marked_at DESC, r.id", (status, status))
    return [dict(r) for r in rows]


# --- explaining --------------------------------------------------------------

def explain(conn: sqlite3.Connection, role_id: int, cfg: RankingConfig,
            now: datetime | None = None) -> dict:
    """Why a role ranks where it does, or why it is not on the list.

    A job posted once per location is one entry, led by its best-scoring
    posting. For one of the job's other postings the answer is the entry's
    rank, with ``listed_under`` naming the posting that leads it.
    """
    now = now or datetime.now().astimezone()
    role = get_role(conn, role_id)
    ev = evaluate(role, cfg, now.astimezone(timezone.utc).date(),
                  in_baseline(role, baseline_fetches(conn)))
    current = latest_shortlist(conn)
    entry = cutoff = held = under = None
    others: list[dict] = []
    if current is not None:
        # The entry (or the role held back) this posting belongs to: itself,
        # or the better-scoring posting of the same job.
        under = conn.execute(
            "SELECT p.lead_role_id AS id, p.score AS own_score, r.location, r.url FROM shortlist_postings p "
            "JOIN roles r ON r.id = p.lead_role_id WHERE p.shortlist_id = ? AND p.role_id = ?",
            (current["id"], role_id)).fetchone()
        lead_id = under["id"] if under else role_id
        held = conn.execute(
            "SELECT score FROM shortlist_held WHERE shortlist_id = ? AND role_id = ?",
            (current["id"], lead_id)).fetchone()
        entry = conn.execute(
            "SELECT rank, score FROM shortlist_entries WHERE shortlist_id = ? AND role_id = ?",
            (current["id"], lead_id)).fetchone()
        cutoff = conn.execute(
            "SELECT rank, score FROM shortlist_entries WHERE shortlist_id = ? ORDER BY rank DESC LIMIT 1",
            (current["id"],)).fetchone()
        others = _other_postings(conn, current["id"]).get(role_id, [])
    marked = SameJob(conn).covering_mark(role) if not role["closed_at"] else None

    if role["closed_at"]:
        why_not = f"the role closed on {role['closed_at'][:10]}"
    elif not role["active"]:
        why_not = f"{role['company']} is deactivated"
    elif role["mark"] in HIDDEN_MARKS:
        why_not = f"you marked it {role['mark']}"
    elif marked is not None:
        why_not = (f"you marked another posting of the same job {marked['mark']} "
                   f"(ID {marked['id']}, {marked['location'] or 'no location given'}), and a mark covers "
                   "the job wherever it is posted")
    elif not ev.eligible:
        why_not = ev.why_not
    elif entry is None and held is not None:
        why_not = (f"held back by the cap of {cfg.max_per_company} roles per company: "
                   f"{role['company']} already has that many higher-ranked roles on the list. "
                   f"It scores {ev.score:g}, enough to be listed otherwise")
    elif entry is None and current is not None and cutoff is not None and cutoff["rank"] >= cfg.size:
        why_not = (f"{cutoff['rank']} roles rank above it: it scores {ev.score:g} and the last "
                   f"role on the list scores {cutoff['score']:g}")
    elif entry is None:
        why_not = "the list has not been rebuilt since this changed; run: rolescout shortlist rebuild"
    else:
        why_not = ""
    return {
        "role": {**dict(role), "backed_by": backed_by(conn).get(role["company_id"], [])},
        "score": ev.score, "detail": ev.detail,
        "rank": entry["rank"] if entry and not why_not else None,
        # The score this posting had when the list was built.
        "listed_score": (under["own_score"] if under else entry["score"]) if entry else None,
        # The posting that leads the entry, when it is another posting of the same job.
        "listed_under": ({"id": under["id"], "location": under["location"], "url": under["url"],
                          "score": (entry or held)["score"]} if under and (entry or held) else None),
        "other_postings": others,
        "list_date": current["list_date"] if current else None,
        "list_size": cutoff["rank"] if cutoff else 0,
        "config_changed": bool(current) and current["config_hash"] != cfg.digest,
        "held_back_by_cap": bool(held is not None and entry is None and ev.eligible
                                 and not role["closed_at"] and role["active"]
                                 and role["mark"] not in HIDDEN_MARKS and marked is None),
        "why_not": why_not,
    }
