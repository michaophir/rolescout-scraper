"""Reading the location text job boards give for a role.

Boards put several places in one field ("Austin, TX | Remote - US",
"London; New York, NY") and write the same place many ways. This module
splits that text into separate locations and decides whether one of them is
in a required place. The only place with built-in knowledge is the United
States: its names, its states and their abbreviations, and its major cities.
Anything else in a requirement is matched as a plain phrase.
"""

import re

US_REGION = "united states"
US_ALIASES = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}

US_CITIES = [
    "New York City", "New York", "NYC", "Brooklyn", "Manhattan", "San Francisco", "SF", "Bay Area",
    "Silicon Valley", "Los Angeles", "Seattle", "Boston", "Austin", "Chicago", "Denver", "Atlanta",
    "Miami", "Dallas", "Houston", "Washington DC", "Washington, DC", "Washington D.C.", "Philadelphia",
    "San Diego", "San Jose", "Palo Alto", "Mountain View", "Sunnyvale", "Menlo Park", "Redwood City",
    "Santa Clara", "San Mateo", "Oakland", "Phoenix", "Salt Lake City", "Nashville", "Raleigh",
    "Durham", "Charlotte", "Minneapolis", "Detroit", "Pittsburgh", "Boulder", "Bellevue", "Kirkland",
    "Redmond", "Irvine", "Santa Monica", "Culver City", "Cupertino", "Jersey City", "Hoboken",
    "Sacramento", "Las Vegas", "Orlando", "Tampa", "Baltimore", "St. Louis", "Kansas City",
    "Indianapolis", "Cincinnati", "Cleveland", "Ann Arbor", "Scottsdale", "Tempe", "Plano",
]

# Places outside the US whose presence means a two-letter code next to them is
# a country code, not a state: "Tel Aviv, IL", "Berlin, DE", "Bengaluru, IN".
NON_US_PLACES = [
    "Canada", "Mexico", "Brazil", "Argentina", "Colombia", "Chile", "Peru", "United Kingdom", "UK",
    "England", "Scotland", "Ireland", "France", "Germany", "Spain", "Portugal", "Italy", "Netherlands",
    "Belgium", "Switzerland", "Austria", "Sweden", "Norway", "Denmark", "Finland", "Poland", "Czechia",
    "Czech Republic", "Romania", "Bulgaria", "Greece", "Turkey", "Ukraine", "Israel", "Egypt",
    "South Africa", "Nigeria", "Kenya", "United Arab Emirates", "UAE", "Saudi Arabia", "India",
    "Pakistan", "China", "Hong Kong", "Taiwan", "Japan", "South Korea", "Korea", "Singapore",
    "Malaysia", "Indonesia", "Philippines", "Vietnam", "Thailand", "Australia", "New Zealand",
    "Europe", "EMEA", "APAC", "LATAM", "Asia", "Africa",
    "London", "Dublin", "Paris", "Berlin", "Munich", "Hamburg", "Amsterdam", "Madrid", "Barcelona",
    "Lisbon", "Milan", "Rome", "Zurich", "Geneva", "Vienna", "Stockholm", "Oslo", "Copenhagen",
    "Helsinki", "Warsaw", "Prague", "Bucharest", "Sofia", "Athens", "Istanbul", "Kyiv", "Tel Aviv",
    "Jerusalem", "Haifa", "Herzliya", "Dubai", "Cairo", "Lagos", "Nairobi", "Johannesburg",
    "Cape Town", "Bengaluru", "Bangalore", "Mumbai", "Delhi", "Hyderabad", "Chennai", "Pune",
    "Gurgaon", "Gurugram", "Shanghai", "Beijing", "Shenzhen", "Tokyo", "Osaka", "Seoul", "Taipei",
    "Jakarta", "Manila", "Bangkok", "Sydney", "Melbourne", "Auckland", "Toronto", "Vancouver",
    "Montreal", "Ottawa", "Calgary", "Mexico City", "Sao Paulo", "São Paulo", "Buenos Aires",
    "Bogota", "Bogotá", "Santiago", "Lima",
]


def _words(phrases) -> re.Pattern:
    """Any of the phrases as whole words, any case, spacing flexible."""
    parts = sorted({r"\s+".join(re.escape(w) for w in p.split()) for p in phrases}, key=len, reverse=True)
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])", re.I)


_US_NAMES = re.compile(
    r"(?<![A-Za-z0-9])(?:United\s+States(?:\s+of\s+America)?|U\.S\.A\.?|U\.S\.|USA)(?![A-Za-z0-9])", re.I)
_US_CODE = re.compile(r"(?<![A-Za-z0-9])US(?![A-Za-z0-9])")                 # capitals only: not "us"
_US_STATE_NAME = _words(US_STATES.values())
_US_STATE_CODE = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(US_STATES) + r")(?![A-Za-z0-9])")  # capitals only
_US_CITY = _words(US_CITIES)
_NON_US = _words(NON_US_PLACES)
_REMOTE = re.compile(r"remote|anywhere|work\s+from\s+home|wfh|distributed|virtual", re.I)
_FILLER = re.compile(
    r"remote(?:ly)?|anywhere|work(?:ing)?\s+from\s+home|wfh|distributed|virtual|friendly|first|only|eligible|"
    r"based|position|role|hybrid|travel|required|optional|possible|ok|okay|within|in|the|or|and|full|fully|"
    r"time|n/a|na|tbd|flexible|multiple|locations?|various|global(?:ly)?|worldwide", re.I)
_SEPARATORS = re.compile(r"\s*[|;\n]\s*|\s+/\s+|\s+or\s+|\s+&\s+|,\s+or\s+", re.I)
_QUALIFIER = _words([*US_STATES.values(), *US_ALIASES, *NON_US_PLACES[:60]])


def split_locations(text: str | None) -> list[str]:
    """The separate places named in a role's location text, in the order given.

    "Austin, TX | Remote - US" gives two; "San Jose, CA, Denver, CO, or Remote"
    gives three. A "City, State" or "City, Country" pair stays together.
    """
    found: list[str] = []
    for chunk in _SEPARATORS.split(text or ""):
        chunk = chunk.strip(" ,")
        if not chunk:
            continue
        pieces = [p.strip() for p in chunk.split(",") if p.strip()]
        current: list[str] = []
        for piece in pieces:
            bare = re.sub(r"\s*\(.*?\)\s*", " ", piece).strip()      # "NY (HQ)" qualifies like "NY"
            is_code = bool(re.fullmatch(r"[A-Z]{2,3}", bare))
            # A code after another code is a list of places ("SF, NY, SEA"),
            # not a place and its state, unless it is a country ("CA, USA").
            after_code = bool(current) and bool(re.fullmatch(r"[A-Z]{2,3}", current[-1]))
            is_country = bare.lower() in US_ALIASES or bare in ("UK", "UAE")
            is_qualifier = (is_code and (not after_code or is_country)) or bool(_QUALIFIER.fullmatch(bare))
            # "New York" is a city as well as a state: after "Austin, TX" or after
            # another city it starts a new place; after "New York" it is the state.
            starts_new = not current or not is_qualifier or (
                piece.lower() == "new york" and current[-1].lower() != "new york"
                and (len(current) >= 2 or bool(_US_CITY.fullmatch(current[-1]))))
            if starts_new and current:
                found.append(", ".join(current))
                current = []
            current.append(piece)
        if current:
            found.append(", ".join(current))
    return list(dict.fromkeys(found))


def is_remote(place: str) -> bool:
    return bool(_REMOTE.search(place or ""))


def names_no_place(place: str) -> bool:
    """True for "Remote", "Remote-friendly (travel required)", "Anywhere": remote
    wording with no country, region or city in it."""
    rest = _FILLER.sub(" ", place or "")
    return not re.search(r"[A-Za-z]{2,}", rest)


def in_united_states(place: str) -> bool:
    """Whether one location is in the United States, in any form boards use:
    the country's names, a state or its two-letter code, or a major city.
    A two-letter code is not trusted when the same location names a place
    outside the US ("Tel Aviv, IL" is not Illinois)."""
    if _US_NAMES.search(place) or _US_CODE.search(place):
        return True
    foreign = bool(_NON_US.search(place))
    if _US_CITY.search(place) and not foreign:
        return True
    if _US_STATE_NAME.search(place) and not foreign:
        return True
    return bool(_US_STATE_CODE.search(place)) and not foreign


class Requirement:
    """A list of places, at least one of which a role must be in.

    "United States" (or USA, US) stands for every US form; any other entry is
    a phrase matched as whole words. A remote location that names no place at
    all satisfies any requirement.
    """

    def __init__(self, places: list[str]) -> None:
        self.places = [p.strip() for p in places if p and p.strip()]
        self.united_states = any(p.lower() in US_ALIASES for p in self.places)
        literal = [p for p in self.places if p.lower() not in US_ALIASES]
        self._literal = _words(literal) if literal else None

    def __bool__(self) -> bool:
        return bool(self.places)

    def describe(self) -> str:
        return ", ".join(self.places)

    def accepts(self, place: str) -> bool:
        if self.united_states and in_united_states(place):
            return True
        if self._literal is not None and self._literal.search(place):
            return True
        return is_remote(place) and names_no_place(place)

    def first_match(self, places: list[str]) -> str | None:
        return next((p for p in places if self.accepts(p)), None)
