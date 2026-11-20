"""Pay as numbers: read from a board's own pay field, or found in a description.

Rules only: no model, no network. A board's pay field is trusted to be about
pay, so any amount in it is read. A description is prose about everything, so
pay is taken from it only when it is clearly pay:

- A range, never a lone figure: two amounts joined by a dash or "to", the
  first with a currency sign or code ("$120,000 - $150,000", "$180K–$220K",
  "USD 90,000 to 110,000", "$25 - $30 per hour").
- The nearest money word before the range, in its sentence, must be a pay
  word (salary, compensation, base, pay range, OTE, wage ...). When the
  nearest one is about other money (revenue, funding, a bonus, a stipend, a
  benefit ...), the range is ignored. With no word before it, a pay word
  right after it counts ("$120K–$150K base salary", "$25–$30 per hour").
- A line of a list that is only a label and a range ("New York: $150,000 -
  $180,000") takes its word from the text that leads into the list.
- Amounts in millions or billions, percentages, and ranges per month, week
  or day are not read. A yearly range is 20,000 to 1,500,000; an hourly one
  is 7 to 400 and must say it is hourly. The top is at most four times the
  bottom.

Several different ranges in one posting are almost always one per location
(or per pay zone), so the result is the lowest and the highest with
``varies`` set. When base pay and on-target earnings are both given, base
pay is kept. Empty is better than wrong: text that fails any rule gives None.
"""

import re
from dataclasses import dataclass

SOURCE_FIELD = "field"                # the board's own pay field
SOURCE_DESCRIPTION = "description"    # found in the description text

# The columns of ``roles`` that hold pay, in the order ``columns`` gives them.
PAY_COLUMNS = ("compensation_text", "pay_min", "pay_max", "pay_currency", "pay_period", "pay_varies", "pay_source")
NO_PAY = ("", None, None, "", "", 0, "")

_CODE = r"USD|CAD|AUD|NZD|SGD|GBP|EUR"
_SIGN = r"US\$|CA\$|C\$|AU\$|A\$|NZ\$|S\$|\$|£|€"
_SIGN_CURRENCY = {"US$": "USD", "CA$": "CAD", "C$": "CAD", "AU$": "AUD", "A$": "AUD", "NZ$": "NZD", "S$": "SGD",
                  "$": "USD", "£": "GBP", "€": "EUR"}
_NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d+)?"
_THOUSANDS = r"(?:\s?(?P<{}>[kK])(?![A-Za-z]))?"
_PER_HOUR = r"per\s+hour|per\s+hr|an\s+hour|hourly|/\s?h(?:ou)?r\b|/\s?h\b"
_PER_YEAR = r"per\s+year|per\s+annum|a\s+year|annual(?:ly|ized)?|yearly|/\s?y(?:ea)?r\b"
_PER_OTHER = r"per\s+(?:month|week|day)|a\s+(?:month|week|day)|monthly|weekly|daily|/\s?(?:mo|month|wk|week|day)\b"
_HOURLY = re.compile(_PER_HOUR, re.I)
_YEARLY = re.compile(_PER_YEAR, re.I)
_OTHER_PERIOD = re.compile(_PER_OTHER, re.I)

# An amount that says its currency: "$120,000", "USD 90,000", "US$ 85K".
_FIRST = re.compile(
    rf"(?<![\w$£€.,])(?:(?P<code>{_CODE})\s?(?P<sign_after_code>{_SIGN})?|(?P<sign>{_SIGN}))\s?"
    rf"(?P<n1>{_NUMBER})" + _THOUSANDS.format("k1"))
# What makes it a range, read from where the first amount ends: an optional
# currency code or period, a dash or "to", and a second amount.
_REST = re.compile(
    rf"\s?(?:(?P<code_mid>{_CODE})\b)?\s?(?:(?:{_PER_HOUR}|{_PER_YEAR})\s?)?\s?"
    rf"(?P<joined>-|–|—|−|to\b|and\b)\s?"
    rf"(?:(?:{_CODE})\s?)?(?:{_SIGN})?\s?(?P<n2>{_NUMBER})" + _THOUSANDS.format("k2")
    + rf"(?:\s?(?P<code_end>{_CODE})\b)?", re.I)
# What follows an amount that is not pay: millions, billions, a percentage,
# or more digits (the number went on).
_NOT_AN_AMOUNT = re.compile(r"\s?(?:mm?\b|bn?\b|million|billion|%)|,?\d", re.I)
_BETWEEN = re.compile(r"between\s*$", re.I)

_PAY_WORD = (
    r"salar(?:y|ies)|compensation|base\s+pay|pay\s+(?:range|scale|band|rate|zone)s?|rate\s+of\s+pay|hourly\s+rate"
    r"|hiring\s+range|base|pay|wages?|on[- ]target(?:\s+earnings)?|earnings")
_TARGET_WORD = re.compile(r"\bOTE\b|on[- ]target|total\s+(?:target\s+)?(?:cash|compensation)|total\s+rewards", re.I)
_OTHER_MONEY = (
    r"revenues?|ARR|MRR|funding|funded|raised|raising|valuation|valued|series\s+[A-H]\b|budgets?|quotas?"
    r"|book\s+of\s+business|deal\s+sizes?|portfolios?|pipeline|AUM|assets|bonus(?:es)?|commissions?|stipends?"
    r"|reimburse\w*|allowances?|401|tuition|relocation|sign[- ]on|signing|referrals?|benefits?|perks?|equity"
    r"|stock|RSUs?|grants?|spend(?:ing)?|savings|invest\w*|insurance|donations?|match(?:ing|es)?|awards?|prizes?"
    r"|scholarships?|wellness|customer\s+base|user\s+base|client\s+base|install\s+base|contracts?|accounts\s+of"
    r"|purchases?|transactions?|loans?|credits?|fees?")
_KEYWORD = re.compile(
    rf"(?<![\w$])(?:(?P<other>{_OTHER_MONEY})|(?P<pay>OTE|{_PAY_WORD})|(?P<per>{_PER_HOUR}|{_PER_YEAR}))(?!\w)", re.I)
# Where a bare "$" is not the US dollar: the country named just before the range.
_DOLLAR_OF = ((re.compile(r"\b(?:CAD|CAN|Canad\w+)\b"), "CAD"), (re.compile(r"\b(?:AUD|Australia\w*)\b"), "AUD"),
              (re.compile(r"\bNew Zealand\b"), "NZD"), (re.compile(r"\bSingapore\b"), "SGD"))
_ADDED_ON = re.compile(r"[+,;&]|\b(?:plus|and|with)\b", re.I)
_SENTENCE_END = re.compile(r"[.!?]\s+(?=[A-Z\"'(•*-])|\n|•|;|\s[–—-]\s(?=[A-Z])")
_LABEL = re.compile(r"[A-Za-z][\w ,/&()'–-]{0,40}:\s*\(?\s*$")
_LOOK_BACK = 600      # how far before a list line its lead-in is looked for
_LOOK_AHEAD = 30      # how far after a range a pay word still belongs to it
_RIGHT_AFTER = 12     # ... when a word about other money stands before the range
_RUN_GAP = 40         # ranges this close together are one list

YEAR_RANGE = (20_000.0, 1_500_000.0)
HOUR_RANGE = (7.0, 400.0)
MAX_SPREAD = 4.0


@dataclass(frozen=True)
class Pay:
    low: float
    high: float
    currency: str       # USD, CAD, ...
    period: str         # "year" or "hour"
    varies: bool = False   # several ranges were given (by location); low and high span them all

    def text(self) -> str:
        """"USD 120,000 - 150,000 per year", the way the pay fields are written."""
        def amount(value: float) -> str:
            return f"{value:,.2f}".rstrip("0").rstrip(".") if value % 1 else f"{value:,.0f}"
        span = amount(self.low) if self.low == self.high else f"{amount(self.low)} - {amount(self.high)}"
        return f"{self.currency} {span} per {self.period}" + (
            " (several ranges, by location or level)" if self.varies else "")

    def short(self) -> str:
        """"USD 120K-150K/yr", for a table column."""
        def amount(value: float) -> str:
            if self.period == "year" and value >= 1000:
                return f"{value / 1000:.1f}".rstrip("0").rstrip(".") + "K"
            return f"{value:.2f}".rstrip("0").rstrip(".")
        span = amount(self.low) if self.low == self.high else f"{amount(self.low)}-{amount(self.high)}"
        return f"{self.currency} {span}/{'yr' if self.period == 'year' else 'hr'}" + ("*" if self.varies else "")


@dataclass(frozen=True)
class _Range:
    low: float
    high: float
    currency: str
    start: int
    end: int
    thousands: bool     # written with K


def _value(number: str, thousands: bool) -> float:
    return float(number.replace(",", "")) * (1000 if thousands else 1)


def _ranges(text: str) -> list[_Range]:
    """Every "amount - amount" in the text whose first amount says its currency."""
    found = []
    for first in _FIRST.finditer(text):
        rest = _REST.match(text, first.end())
        if rest is None or _NOT_AN_AMOUNT.match(text, first.end()) or _NOT_AN_AMOUNT.match(text, rest.end("n2") if not rest.group("k2") else rest.end("k2")):
            continue
        if rest.group("joined").lower() == "and" and not _BETWEEN.search(text, max(0, first.start() - 12), first.start()):
            continue
        k1, k2 = bool(first.group("k1")), bool(rest.group("k2"))
        low_text, high_text = first.group("n1"), rest.group("n2")
        # "$180–220K" and "$180K–220": the K belongs to both.
        low = _value(low_text, k1 or (k2 and float(low_text.replace(",", "")) < 1000))
        high = _value(high_text, k2 or (k1 and float(high_text.replace(",", "")) < 1000))
        code = first.group("code") or rest.group("code_mid") or rest.group("code_end")
        sign = first.group("sign") or first.group("sign_after_code") or ""
        currency = code.upper() if code else _SIGN_CURRENCY.get(sign.upper(), "")
        if currency:
            found.append(_Range(low, high, currency, first.start(), rest.end(), k1 or k2))
    return found


def _period(text: str, found: _Range, start: int, end: int) -> str:
    """"year", "hour", or "" when the range is neither or cannot be told."""
    around = text[max(start, found.start - 60):min(end, found.end + 40)]
    low, high = found.low, found.high
    if not 0 < low <= high or high > MAX_SPREAD * low:
        return ""
    if YEAR_RANGE[0] <= low and high <= YEAR_RANGE[1]:
        # By the size alone; "per month" beside it says otherwise.
        return "" if _OTHER_PERIOD.search(around) and not _YEARLY.search(around) else "year"
    if HOUR_RANGE[0] <= low and high <= HOUR_RANGE[1] and not found.thousands and _HOURLY.search(around):
        return "hour"
    return ""


def _sentence(text: str, found: _Range) -> tuple[int, int]:
    start = 0
    for stop in _SENTENCE_END.finditer(text, max(0, found.start - 600), found.start):
        start = stop.end()
    after = _SENTENCE_END.search(text, found.end, found.end + 400)
    return max(start, found.start - 600), after.start() if after else min(len(text), found.end + 400)


def _governing(text: str, found: _Range, start: int, end: int, previous_end: int) -> re.Match | None:
    """The money word that says what a range is about: the nearest one before
    it in its sentence, else a pay word right after it."""
    def last_word(since: int) -> re.Match | None:
        last = None
        for word in _KEYWORD.finditer(text, since, found.start):
            # An aside in brackets ("compensation (salary + commission) range
            # is $A-$B") does not say what the range is about.
            rest = text[word.end():found.start]
            if rest.count(")") <= rest.count("("):
                last = word
        return last

    before = last_word(max(start, previous_end))
    if before is None and previous_end > start:
        # The second range of a sentence ("... is $A-$B in Denver and $C-$D
        # in Austin") is about what the first one is about.
        before = last_word(start)
    if before is None and start > 0 and not text[start:found.start].strip(" \t(:"):
        # A range that opens its sentence is the figure for what the sentence
        # before it was about ("... the base salary range in Colorado. $A-$B").
        return last_word(max(0, start - 300))
    # The word after the range that says what it is: the first that is not
    # just a period. "$120K-$150K base salary plus equity" is pay; "$100K-
    # $500K in annual revenue" is not.
    # A word about other money that is added on ("$A-$B + 20% bonus + equity",
    # "$A-$B, plus benefits") does not say what the range is.
    following = [word for word in _KEYWORD.finditer(text, found.end, min(end, found.end + _LOOK_AHEAD))
                 if not (word.group("other") and _ADDED_ON.search(text, found.end, word.start()))]
    after = next((word for word in following if not word.group("per")), following[0] if following else None)
    if before is None and after is not None and after.group("other"):
        return None
    if before is not None:
        # "a bonus, and $120K-$150K base salary": the word that follows wins
        # only over a word about other money, and only when it names pay and
        # stands right after the range. "per year" is not enough: a bonus
        # can be per year too.
        if before.group("other") and after is not None and after.group("pay") \
                and after.start() - found.end <= _RIGHT_AFTER:
            return after
        return before
    if after is not None and not after.group("other"):
        return after
    if _LABEL.search(text, max(0, found.start - 80), found.start):
        # A list line, "New York: $150,000 - $180,000": what the list is
        # about was said before it.
        return last_word(max(0, found.start - _LOOK_BACK))
    return None


def from_description(text: str | None) -> Pay | None:
    """The pay a description states, or None. See the module docstring for the rules."""
    text = text or ""
    if not any(sign in text for sign in ("$", "£", "€")) and not re.search(_CODE, text):
        return None
    read: list[tuple[_Range, str, re.Match | None]] = []     # range, period, the word that governs it
    previous_end = 0
    for found in _ranges(text):
        start, end = _sentence(text, found)
        period = _period(text, found, start, end)
        word = _governing(text, found, start, end, previous_end if previous_end > start else start) if period else None
        previous_end = found.end
        if found.currency == "USD" and "$" in text[found.start:found.end] and not re.search(
                r"US\$|USD", text[max(0, found.start - 12):found.end + 8]):
            lead = text[max(start, found.start - 60, 0):found.start]
            currency = next((code for pattern, code in _DOLLAR_OF if pattern.search(lead)), "USD")
            found = _Range(found.low, found.high, currency, found.start, found.end, found.thousands)
        read.append((found, period, word))
    accepted: list[tuple[_Range, str, bool]] = []     # range, period, whether it is on-target pay
    run: list[tuple[_Range, str, re.Match | None]] = []
    for item in [*read, None]:
        if item is not None and (not run or item[0].start - run[-1][0].end <= _RUN_GAP):
            run.append(item)
            continue
        # A run of ranges close together is one list ("Zone 1: $A-$B Zone 2:
        # $C-$D ... pay zones"): what any of them is about, all of them are
        # about, unless one of them is about other money.
        words = [word for _, _, word in run if word is not None]
        if words and not any(word.group("other") for word in words):
            # On-target earnings, when the word that governs the range says so.
            target = all(_TARGET_WORD.search(text, max(0, word.start() - 30), word.end() + 30) for word in words)
            accepted.extend((found, period, bool(target)) for found, period, _ in run if period)
        run = [item] if item is not None else []
    if not accepted:
        return None
    if any(not target for _, _, target in accepted):
        accepted = [item for item in accepted if not item[2]]      # base pay over on-target earnings
    first, period, _ = accepted[0]
    same = [found for found, p, _ in accepted if p == period and found.currency == first.currency]
    spans = {(found.low, found.high) for found in same}
    return Pay(min(low for low, _ in spans), max(high for _, high in spans), first.currency, period, len(spans) > 1)


_LONE = re.compile(_FIRST.pattern + rf"(?:\s?(?P<code_end>{_CODE})\b)?")


def from_field(text: str | None) -> Pay | None:
    """The numbers in a board's pay field, or None when it has none that can
    be read as yearly or hourly pay. The field is about pay, so no pay word
    is asked for and a single amount counts."""
    text = text or ""
    found = _ranges(text)
    if not found:
        for lone in _LONE.finditer(text):
            if _NOT_AN_AMOUNT.match(text, lone.end("k1") if lone.group("k1") else lone.end("n1")):
                continue
            code = lone.group("code") or lone.group("code_end")
            currency = code.upper() if code else _SIGN_CURRENCY.get((lone.group("sign") or lone.group("sign_after_code") or "").upper(), "")
            value = _value(lone.group("n1"), bool(lone.group("k1")))
            if currency:
                found.append(_Range(value, value, currency, lone.start(), lone.end(), bool(lone.group("k1"))))
    def period(item: _Range) -> str:
        # In a pay field a small amount with no period beside it is an hourly rate.
        hourly = (HOUR_RANGE[0] <= item.low <= item.high <= HOUR_RANGE[1] and not item.thousands
                  and not _OTHER_PERIOD.search(text) and not _YEARLY.search(text))
        return _period(text, item, 0, len(text)) or ("hour" if hourly else "")

    read = [(item, period(item)) for item in found]
    read = [(item, period) for item, period in read if period]
    if not read:
        return None
    first, period = read[0]
    spans = {(item.low, item.high) for item, p in read if p == period and item.currency == first.currency}
    return Pay(min(low for low, _ in spans), max(high for _, high in spans), first.currency, period, len(spans) > 1)


def columns(field_text: str | None, description: str | None) -> tuple:
    """The values of ``PAY_COLUMNS`` for a role: the board's field when it
    gave one, else what the description states, else nothing."""
    field_text = (field_text or "").strip()
    if field_text:
        pay = from_field(field_text)
        if pay is None:
            return (field_text, None, None, "", "", 0, SOURCE_FIELD)
        return (field_text, pay.low, pay.high, pay.currency, pay.period, int(pay.varies), SOURCE_FIELD)
    pay = from_description(description)
    if pay is None:
        return NO_PAY
    return (pay.text(), pay.low, pay.high, pay.currency, pay.period, int(pay.varies), SOURCE_DESCRIPTION)


def of(row) -> Pay | None:
    """The pay stored on a role row (anything with the ``PAY_COLUMNS`` keys), as numbers."""
    try:
        if row["pay_min"] is None or not row["pay_period"]:
            return None
        return Pay(row["pay_min"], row["pay_max"], row["pay_currency"], row["pay_period"], bool(row["pay_varies"]))
    except (KeyError, IndexError):
        return None


def shown(row, width: int = 0) -> str:
    """Pay for a table cell, with where it came from: "USD 120K-150K/yr
    (description)". A board field that could not be read as numbers is shown
    as written."""
    try:
        text, source = row["compensation_text"] or "", row["pay_source"] or ""
    except (KeyError, IndexError):
        return ""
    if not text:
        return ""
    pay = of(row)
    label = pay.short() if pay else (text if not width or len(text) <= width else text[:width - 1].rstrip() + "…")
    return f"{label} ({'board' if source == SOURCE_FIELD else 'description'})" if source else label


def summary(row) -> dict | None:
    """Pay as results give it: the text, the numbers when it could be read
    as numbers, and where it came from ("field": the board's own pay field;
    "description": found in the description text). None when a role has none."""
    try:
        text = row["compensation_text"] or ""
    except (KeyError, IndexError):
        return None
    if not text:
        return None
    pay = of(row)
    return {"text": text, "min": pay.low if pay else None, "max": pay.high if pay else None,
            "currency": pay.currency if pay else None, "period": pay.period if pay else None,
            "several_ranges": bool(pay and pay.varies), "source": row["pay_source"] or None}


def line(row) -> str:
    """Pay as one sentence part: "USD 120,000 - 150,000 per year (found in the description)"."""
    try:
        text, source = row["compensation_text"] or "", row["pay_source"] or ""
    except (KeyError, IndexError):
        return ""
    where = {SOURCE_FIELD: "the board's pay field", SOURCE_DESCRIPTION: "found in the description"}.get(source)
    return f"{text} ({where})" if text and where else text
