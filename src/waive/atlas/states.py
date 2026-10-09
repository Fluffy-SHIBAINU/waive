"""US state names and codes, shared by the structurer and the verifier."""

import re

US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "DC": "district of columbia",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho", "IL": "illinois",
    "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon",
    "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia",
    "WA": "washington", "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
}  # fmt: skip
CODE_OF_NAME = {name: code for code, name in US_STATES.items()}
# Longest names first, so "west virginia" is one state, not two.
STATE_NAME = "|".join(sorted(map(re.escape, US_STATES.values()), key=len, reverse=True))
STATE_NAMES = re.compile(rf"\b({STATE_NAME})\b")


def states_named(text: str) -> set[str]:
    """The codes of the state names written out as whole words in `text` (any case)."""
    return {CODE_OF_NAME[match.group(1)] for match in STATE_NAMES.finditer(text.lower())}
