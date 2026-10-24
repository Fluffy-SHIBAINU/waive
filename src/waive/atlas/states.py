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
STATE_CODE = "|".join(US_STATES)
# Codes count only as upper-case tokens inside mixed-case text ("KS, MO or IL") ...
STATE_CODES = re.compile(rf"\b({STATE_CODE})\b")
# ... and the ones that are English words only inside a list of codes: "Residents of Indiana OR
# Illinois" names two states, not Oregon (review of 7.9).
WORD_CODES = frozenset({"OR", "IN", "ME", "OK", "HI"})
CODE_LIST = re.compile(
    rf"\b(?:{STATE_CODE})(?:\s*,\s*(?:{STATE_CODE}))*\s*,?\s+(?i:and|or|&)\s+(?:{STATE_CODE})\b"
)


def states_named(text: str) -> set[str]:
    """The codes of the state names written out as whole words in `text` (any case)."""
    return {CODE_OF_NAME[match.group(1)] for match in STATE_NAMES.finditer(text.lower())}


def code_tokens(text: str) -> list[re.Match[str]]:
    """The state codes written as upper-case tokens in `text`, where they appear; a code that is
    also an English word ("OR", "IN") counts only inside a list of codes. The caller decides
    whether the text is mixed-case enough for codes to mean anything."""
    listed: set[str] = set()
    for match in CODE_LIST.finditer(text):
        listed.update(STATE_CODES.findall(match.group(0)))
    return [
        match
        for match in STATE_CODES.finditer(text)
        if match.group(1) not in WORD_CODES or match.group(1) in listed
    ]
