"""Company profiles: description, sectors, stage, employee count and HQ.

One row per tracked company in ``company_profiles``. Every field records
where its value came from (board, agent or manual) and when. A value set by
hand is never replaced by board or agent data; a value an agent set is not
replaced by board data.

Stage is one of a fixed list; the board's own word for it is kept beside it.
Sectors are a short controlled list. Raw board tags are mapped onto it by a
TOML file next to the database (``sectors.toml``), or by the built-in rules
until that file exists. The raw tags are always kept.
"""

import json
import os
import re
import sqlite3
import tomllib
from pathlib import Path

from rolescout.db import transaction, utc_now

FIELDS = ("description", "sectors", "stage", "employees", "hq")
SOURCES = ("board", "agent", "manual")
_RANK = {"board": 0, "agent": 1, "manual": 2}      # a source never replaces a higher one

# --- stage ---------------------------------------------------------------------

STAGES = ("bootstrapped", "pre-seed", "seed", "series A", "series B", "series C", "series D",
          "series E+", "late-stage private", "public", "acquired", "unknown")
# What boards call a stage, squashed to letters and digits.
_BOARD_STAGES = {
    "bootstrapped": "bootstrapped", "preseed": "pre-seed", "seed": "seed",
    "seriesa": "series A", "seriesb": "series B", "seriesc": "series C", "seriesd": "series D",
    **{f"series{letter}": "series E+" for letter in "efghij"},
    "privateequity": "late-stage private", "latestage": "late-stage private", "latestageprivate": "late-stage private",
    "ipo": "public", "public": "public", "postipo": "public",
    "acquisition": "acquired", "acquired": "acquired",
    "undisclosed": "unknown", "other": "unknown", "seriesunknown": "unknown", "unknown": "unknown",
}


def _squash(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def map_stage(raw: object) -> str:
    """The fixed-list stage for a board's value ("series_f" is "series E+").
    Anything not recognized, or nothing, is "unknown"."""
    key = _squash(raw)
    if key in _BOARD_STAGES:
        return _BOARD_STAGES[key]
    for stage in STAGES:                       # "Series E and later", "series e+", "Series A"
        if key == _squash(stage) or (stage == "series E+" and key in ("serieseandlater", "seriese")):
            return stage
    return "unknown"


def parse_stage(text: str) -> str:
    """A stage typed by hand. Raises ``ProfileError`` unless it is on the list."""
    key = _squash(text)
    if key in _BOARD_STAGES or key in {_squash(s) for s in STAGES} or key == "serieseandlater":
        return map_stage(text)
    raise ProfileError(f"{text!r} is not a stage; use one of: {', '.join(STAGES)}")


# --- sectors -------------------------------------------------------------------

MAPPING_NAME = "sectors.toml"
MAPPING_ENV_VAR = "ROLESCOUT_SECTORS"
# The built-in list and the words that place a raw tag in a sector. Used to
# write the starter mapping, and directly until a mapping file exists.
BUILT_IN = {
    "health tech": r"health|medic|clinic|biotech|pharma|life scien|wellness|therap|dental|patient|genom|diagnos|care\b|fitness",
    "fintech": r"fin ?tech|financ|payment|bank|insur|lending|credit|crypto|blockchain|web3|trading|wealth|accounting|invest|tax\b|defi\b",
    "AI and ML": r"\bai\b|artificial intelligence|machine learning|\bml\b|deep learning|generative|nlp|natural language|computer vision|robot|llm",
    "developer tools": r"developer|dev ?ops|\bapi\b|\bapis\b|open source|software development|low code|no code|testing|\bide\b|\bsdk\b",
    "data and infrastructure": r"data\b|database|analytics|cloud|infrastructure|\biot\b|semiconductor|hardware|network|storage|comput|big data|business intelligence|telecom",
    "security": r"security|cyber|privacy|fraud|identity|compliance|risk management",
    "media and adtech": r"media|advertis|ad ?tech|marketing|content|video|music|gaming|games|entertainment|social|publishing|news|creator|streaming",
    "commerce": r"commerce|retail|marketplace|shopping|consumer goods|fashion|food|beverage|restaurant|delivery|travel|hospitality|\bcpg\b|beauty",
    "HR and work": r"\bhr\b|human resources|recruit|hiring|talent|workforce|payroll|future of work|collaboration|productivity|employee|staffing",
    "education": r"educat|ed ?tech|e-?learning|online learning|training|school|universit",
    "climate and energy": r"climate|energy|clean ?tech|sustainab|solar|carbon|environment|renewable|green|agri|water\b",
    "industrial and logistics": r"logistic|supply chain|manufactur|industrial|transport|automotive|mobility|aerospace|construction|real estate|prop ?tech|shipping|freight|drone|space\b|defense",
}
SECTORS = tuple(BUILT_IN)
_BUILT_IN = [(sector, re.compile(pattern, re.I)) for sector, pattern in BUILT_IN.items()]


class ProfileError(Exception):
    """A profile request that cannot be carried out; the message says why."""


class MappingError(Exception):
    """The sector mapping file cannot be used; the message says why."""


def _tag_key(tag: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+&]+", " ", str(tag or "").lower())).strip()


class SectorMapping:
    """Raw tags to sectors. ``sectors`` is the controlled list, in order."""

    def __init__(self, table: dict[str, list[str]] | None = None, source: str = "") -> None:
        self.source = source
        self.from_file = table is not None
        self.sectors = tuple(table) if table is not None else SECTORS
        self._by_tag: dict[str, list[str]] = {}
        for sector, tags in (table or {}).items():
            for tag in tags:
                found = self._by_tag.setdefault(_tag_key(tag), [])
                if sector not in found:
                    found.append(sector)

    def sectors_of(self, tag: str) -> list[str]:
        """The sectors one raw tag belongs to; [] when it is not mapped."""
        if self.from_file:
            return list(self._by_tag.get(_tag_key(tag), []))
        text = _tag_key(tag)
        return [sector for sector, pattern in _BUILT_IN if pattern.search(text)]

    def apply(self, tags: list[str]) -> list[str]:
        """The sectors for a company's raw tags, in the list's own order."""
        found = {sector for tag in tags for sector in self.sectors_of(tag)}
        return [sector for sector in self.sectors if sector in found]

    def parse(self, text: str) -> list[str]:
        """Sectors typed by hand, comma-separated. Raises ``ProfileError`` for one not on the list."""
        by_key = {_squash(s): s for s in self.sectors}
        chosen = []
        for part in text.split(","):
            if not part.strip():
                continue
            sector = by_key.get(_squash(part))
            if sector is None:
                raise ProfileError(f"{part.strip()!r} is not a sector; use: {', '.join(self.sectors)}")
            if sector not in chosen:
                chosen.append(sector)
        return [sector for sector in self.sectors if sector in chosen]


def mapping_path(db_path: str | os.PathLike, flag: str | os.PathLike | None = None) -> Path:
    """Where the sector mapping lives: --sectors, else $ROLESCOUT_SECTORS, else next to the database."""
    chosen = flag or os.environ.get(MAPPING_ENV_VAR)
    return Path(chosen).expanduser() if chosen else Path(db_path).expanduser().parent / MAPPING_NAME


def parse_mapping(text: str, source: str = "") -> SectorMapping:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MappingError(f"{source or 'sector mapping'} is not valid TOML: {exc}") from None
    table = raw.get("sectors")
    if set(raw) - {"sectors"} or not isinstance(table, dict) or not table:
        raise MappingError(f"{source or 'sector mapping'} must have one [sectors] table: "
                           'each line a sector and its raw tags, e.g. "fintech" = ["Payments", "Banking"]')
    for sector, tags in table.items():
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise MappingError(f'sector "{sector}" must be given a list of tags in quotes')
    return SectorMapping(table, source)


def load_mapping(conn: sqlite3.Connection, path: str | os.PathLike | None = None) -> SectorMapping:
    """The mapping file next to the database (or at ``path``), or the built-in
    rules when there is none yet."""
    if path is None:
        db_file = conn.execute("PRAGMA database_list").fetchone()[2] or ""
        if not db_file:
            return SectorMapping()
        path = mapping_path(db_file)
    path = Path(path)
    if not path.exists():
        return SectorMapping()
    return parse_mapping(path.read_text(encoding="utf-8"), str(path))


def tag_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Every raw sector tag in the database with the number of companies
    carrying it: candidates, plus tracked companies no candidate stands for."""
    counts: dict[str, int] = {}
    rows = [r["sectors"] for r in conn.execute("SELECT sectors FROM candidates")]
    rows += [r["sector_tags"] for r in conn.execute(
        "SELECT p.sector_tags FROM company_profiles p WHERE NOT EXISTS "
        "(SELECT 1 FROM candidates c WHERE c.company_id = p.company_id)")]
    for raw in rows:
        for tag in dict.fromkeys(json.loads(raw or "[]")):
            if str(tag).strip():
                counts[str(tag).strip()] = counts.get(str(tag).strip(), 0) + 1
    return counts


def unmapped_tags(conn: sqlite3.Connection, mapping: SectorMapping) -> list[tuple[str, int]]:
    """Raw tags the mapping places in no sector, most common first."""
    rows = [(tag, n) for tag, n in tag_counts(conn).items() if not mapping.sectors_of(tag)]
    return sorted(rows, key=lambda item: (-item[1], item[0].lower()))


def starter_mapping(conn: sqlite3.Connection) -> str:
    """The text of a starter sectors.toml: the tags present in the database,
    placed by the built-in rules; the ones no rule places are listed at the end."""
    built_in = SectorMapping()
    counts = tag_counts(conn)
    placed: dict[str, list[str]] = {sector: [] for sector in SECTORS}
    loose = []
    for tag in sorted(counts, key=lambda t: (-counts[t], t.lower())):
        sectors = built_in.sectors_of(tag)
        for sector in sectors:
            placed[sector].append(tag)
        if not sectors:
            loose.append(tag)

    def quoted(tags: list[str]) -> str:
        lines, line = [], "   "
        for tag in tags:
            item = " " + json.dumps(tag, ensure_ascii=False) + ","
            if len(line) + len(item) > 100:
                lines.append(line)
                line = "   "
            line += item
        return "\n".join([*lines, line]) if tags else ""
    out = [
        "# rolescout sector mapping: which raw board tags belong to which sector.",
        "#",
        "# This file is yours. The names on the left are your sector list, in the order",
        "# they are shown; add, rename or remove them freely. A tag may sit under more",
        "# than one sector. Capitals, spacing and punctuation in a tag do not matter.",
        "# After editing, run: rolescout sectors apply   (no network)",
        "# Tags no line claims are listed by: rolescout sectors unmapped",
        "",
        "[sectors]",
    ]
    for sector in SECTORS:
        tags = placed[sector]
        out.append(f"{json.dumps(sector)} = [" + ("\n" + quoted(tags) + "\n]" if tags else "]"))
    if loose:
        out += ["", f"# Not placed by the starter rules ({len(loose)}), most common first. Move the ones you",
                "# care about into a sector above:"]
        out += ["#" + line for line in quoted(loose).splitlines()]
    return "\n".join(out) + "\n"


# --- reading and writing profiles ----------------------------------------------

def _company(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT id, name FROM companies WHERE name = ?", (str(name).strip(),)).fetchone()
    if row is None:
        raise ProfileError(f"no company named {name!r}")
    return row


def _row(conn: sqlite3.Connection, company_id: int) -> sqlite3.Row:
    conn.execute("INSERT OR IGNORE INTO company_profiles (company_id) VALUES (?)", (company_id,))
    return conn.execute("SELECT * FROM company_profiles WHERE company_id = ?", (company_id,)).fetchone()


def _write(conn: sqlite3.Connection, company_id: int, field: str, source: str, now: str, **columns) -> bool:
    """Set one field unless a higher source already set it. Must run inside a
    transaction. Returns whether anything was written."""
    row = _row(conn, company_id)
    held = row[f"{field}_source"]
    if held is not None and _RANK[held] > _RANK[source]:
        return False
    if held == source and all(row[k] == v for k, v in columns.items()):
        return False                      # unchanged: keep the date it was first set
    sets = ", ".join(f"{k} = ?" for k in columns)
    conn.execute(f"UPDATE company_profiles SET {sets}, {field}_source = ?, {field}_set_at = ? WHERE company_id = ?",
                 (*columns.values(), source, now, company_id))
    return True


def set_field(conn: sqlite3.Connection, name: str, field: str, value: object, *, source: str = "manual",
              mapping: SectorMapping | None = None) -> dict:
    """Set one profile field by hand or for an agent. Returns the profile."""
    if field not in FIELDS:
        raise ProfileError(f"{field!r} is not a profile field; use one of: {', '.join(FIELDS)}")
    if source not in ("agent", "manual"):
        raise ProfileError("a profile field is set by hand (manual) or by an agent")
    company = _company(conn, name)
    text = str(value if value is not None else "").strip()
    if not text:
        raise ProfileError(f"{field} is empty; to remove a value use: rolescout profile clear NAME {field}")
    if field == "stage":
        columns = {"stage": parse_stage(text), "stage_raw": text}
    elif field == "employees":
        digits = text.replace(",", "")
        if not digits.isdigit():
            raise ProfileError(f"employees must be a whole number, got {text!r}")
        columns = {"employees": int(digits)}
    elif field == "sectors":
        chosen = (mapping or load_mapping(conn)).parse(text)
        if not chosen:
            raise ProfileError("name at least one sector")
        columns = {"sectors": json.dumps(chosen, ensure_ascii=False)}
    else:
        columns = {field: text}
    with transaction(conn):
        written = _write(conn, company["id"], field, source, utc_now(), **columns)
    if not written and source == "agent":
        held = get_profile(conn, name)[field]
        if held["source"] == "manual":
            raise ProfileError(f"{company['name']}: {field} was set by hand and is not replaced by an agent")
    return get_profile(conn, name)


def clear_field(conn: sqlite3.Connection, name: str, field: str) -> bool:
    """Forget a field's value and source, so board data can fill it again at
    the next backfill. Returns whether there was anything to forget."""
    if field not in FIELDS:
        raise ProfileError(f"{field!r} is not a profile field; use one of: {', '.join(FIELDS)}")
    company = _company(conn, name)
    resets = {"description": "description = NULL", "stage": "stage = NULL, stage_raw = NULL",
              "employees": "employees = NULL", "hq": "hq = NULL", "sectors": "sectors = '[]'"}[field]
    with transaction(conn):
        had = _row(conn, company["id"])[f"{field}_source"] is not None
        conn.execute(f"UPDATE company_profiles SET {resets}, {field}_source = NULL, {field}_set_at = NULL "
                     "WHERE company_id = ?", (company["id"],))
        backfill(conn)
    return had


def get_profile(conn: sqlite3.Connection, name: str) -> dict:
    """A company's profile: for each field its value, source and date."""
    company = _company(conn, name)
    row = conn.execute("SELECT * FROM company_profiles WHERE company_id = ?", (company["id"],)).fetchone()
    out: dict = {"company": company["name"]}
    for field in FIELDS:
        value = row[field] if row is not None else None
        if field == "sectors":
            value = json.loads(value or "[]")
        out[field] = {"value": value, "source": row[f"{field}_source"] if row is not None else None,
                      "set_at": row[f"{field}_set_at"] if row is not None else None}
    out["stage"]["raw"] = row["stage_raw"] if row is not None else None
    out["sectors"]["raw_tags"] = json.loads(row["sector_tags"]) if row is not None else []
    return out


def coverage(conn: sqlite3.Connection) -> dict:
    """How many active companies have each profile field filled."""
    total = conn.execute("SELECT COUNT(*) FROM companies WHERE active = 1").fetchone()[0]
    row = conn.execute(
        "SELECT COUNT(p.description) AS description, SUM(p.sectors != '[]') AS sectors, "
        "SUM(p.stage IS NOT NULL AND p.stage != 'unknown') AS stage, COUNT(p.employees) AS employees, "
        "COUNT(p.hq) AS hq, SUM(p.sector_tags != '[]') AS with_tags "
        "FROM companies c JOIN company_profiles p ON p.company_id = c.id WHERE c.active = 1").fetchone()
    return {"companies": total, **{k: row[k] or 0 for k in row.keys()}}


# --- backfill ------------------------------------------------------------------

def backfill(conn: sqlite3.Connection, mapping: SectorMapping | None = None) -> int:
    """Copy what linked candidates know into the profiles of the companies
    they stand for, and bring the board-made VC links up to date. Reads only
    the database. Must run inside a transaction. Returns companies touched.

    Values set by hand or by an agent are left alone. Sectors that came from
    board tags are mapped again, so a changed mapping takes effect here.
    """
    from rolescout import vcs    # vcs builds on the same tables

    mapping = mapping or load_mapping(conn)
    now = utc_now()
    touched = 0
    columns = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    description = "description" if "description" in columns else "NULL AS description"
    for cand in conn.execute(
            f"SELECT company_id, stage, employees, sectors, locations, {description} FROM candidates "
            "WHERE company_id IS NOT NULL ORDER BY id").fetchall():
        company_id = cand["company_id"]
        tags = [str(t) for t in json.loads(cand["sectors"] or "[]") if str(t).strip()]
        places = [str(p) for p in json.loads(cand["locations"] or "[]") if str(p).strip()]
        _row(conn, company_id)
        conn.execute("UPDATE company_profiles SET sector_tags = ? WHERE company_id = ? AND sector_tags != ?",
                     (json.dumps(tags, ensure_ascii=False), company_id, json.dumps(tags, ensure_ascii=False)))
        wrote = False
        if cand["stage"]:
            wrote |= _write(conn, company_id, "stage", "board", now,
                            stage=map_stage(cand["stage"]), stage_raw=cand["stage"])
        if cand["employees"] is not None:
            wrote |= _write(conn, company_id, "employees", "board", now, employees=cand["employees"])
        if places:
            wrote |= _write(conn, company_id, "hq", "board", now, hq=places[0])
        if (cand["description"] or "").strip():
            wrote |= _write(conn, company_id, "description", "board", now, description=cand["description"].strip())
        touched += wrote
    # Board-sourced sectors follow the mapping, for every company with raw tags.
    for row in conn.execute("SELECT company_id, sector_tags, sectors_source FROM company_profiles "
                            "WHERE sector_tags != '[]' AND COALESCE(sectors_source, 'board') = 'board'").fetchall():
        mapped = mapping.apply(json.loads(row["sector_tags"]))
        touched += _write(conn, row["company_id"], "sectors", "board", now,
                          sectors=json.dumps(mapped, ensure_ascii=False))
    vcs.sync_board_links(conn)
    return touched
