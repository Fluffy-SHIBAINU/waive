"""Match an extracted bill to a registry hospital (spec §9 step 4)."""

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from rapidfuzz import fuzz

from waive.atlas.schema import HospitalRef
from waive.cases.extract import BillExtract

CONFIDENT_SCORE = 75.0
LEAD = 10.0


@dataclass(frozen=True)
class MatchCandidate:
    ccn: str
    name: str
    score: float


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def fap_domain(url: str | None) -> str:
    """The bare host of a financial-assistance web address ("" when there is none): all the
    matcher uses, and all a scout request keeps (a printed URL may carry an account number)."""
    if not url:
        return ""
    if "//" not in url:
        url = "https://" + url
    return urlparse(url).netloc.lower().removeprefix("www.")


_domain = fap_domain


def _normalize_name(name: str) -> str:
    text = name.lower().replace("st.", "saint").replace("ctr", "center").replace("med ", "medical ")
    return re.sub(r"[^a-z0-9 ]", " ", text)


def match_hospital(extract: BillExtract, hospitals: list[HospitalRef]) -> list[MatchCandidate]:
    if not extract.hospital_name and not extract.hospital_phone and not extract.fap_url:
        return []
    bill_name = _normalize_name(extract.hospital_name or "")
    bill_phones = {_digits(extract.hospital_phone), _digits(extract.fap_phone)} - {""}
    bill_domain = _domain(extract.fap_url)
    candidates = []
    for hospital in hospitals:
        score = 0.0
        if bill_name:
            score += 0.6 * fuzz.token_set_ratio(bill_name, _normalize_name(hospital.name))
        if hospital.phone and _digits(hospital.phone) in bill_phones:
            score += 30
        if bill_domain and hospital.website_domain:
            if bill_domain == hospital.website_domain:
                score += 30
            elif bill_domain.endswith("." + hospital.website_domain):
                score += 15
        if extract.hospital_address and hospital.city.lower() in extract.hospital_address.lower():
            score += 5
        if score > 0:
            candidates.append(MatchCandidate(hospital.ccn, hospital.name, round(score, 1)))
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:3]


def is_confident(candidates: list[MatchCandidate]) -> bool:
    if not candidates or candidates[0].score < CONFIDENT_SCORE:
        return False
    return len(candidates) == 1 or candidates[0].score - candidates[1].score >= LEAD
