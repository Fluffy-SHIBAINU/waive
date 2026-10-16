"""Who each registry hospital is to the IRS and to PECOS (Phase 10, lane "crosswalk"): the CCN
to EIN and NPI crosswalk that every other ledger lane keys on.

Two free files. RTI's Community Benefit Insight lists nonprofit hospitals with their EIN and,
for most, their CCN; CMS Hospital Enrollments lists every enrolled hospital's NPI and the PECOS
associate id of the legal entity that enrolled it. A CBI row naming the CCN is the best evidence;
failing that, a sibling CCN enrolled by the same legal entity shares its EIN; failing that, a
CBI row with the same normalised name and ZIP, then a fuzzy name in the same city and ZIP. What
is left goes to a CSV for the founder to resolve by hand with `set_manual`, and a hand-set row
is never overwritten by a later pass."""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from waive.atlas import repo as atlas_repo
from waive.db import HospitalRow
from waive.ledger import download
from waive.ledger import repo as ledger_repo
from waive.ledger.tables import HospitalIdRow

log = logging.getLogger(__name__)

CBI_URL = "https://www.communitybenefitinsight.org/api/get_hospitals.php?state={state}"
CBI_SOURCE = "Community Benefit Insight"
ENROLLMENTS_TITLE = "Hospital Enrollments"
# A fuzzy name match in the same city and ZIP counts only from this token_set_ratio up. The ZIP
# is required because token_set_ratio scores 100 whenever one name's words are a subset of the
# other's: "BOSTON MEDICAL CENTER" normalises to "BOSTON", which every Boston hospital contains.
FUZZY_THRESHOLD = 90.0
CONFIDENCE = {"cbi": 1.0, "enrollments": 0.9, "name_zip": 0.8, "fuzzy": 0.7, "manual": 1.0}
# Words that say nothing about which hospital a name means; the two sources disagree on them.
COMMON_WORDS = frozenset(
    "THE INC INCORPORATED HOSPITAL MEDICAL CENTER CORP CORPORATION OF AND & LLC HEALTH "
    "SYSTEM DBA".split()
)
UNMATCHED_COLUMNS = ("ccn", "name", "city", "zip", "best_name", "best_ein", "best_score")


def normalise_ein(text: str | None) -> str | None:
    """Nine digits, or None: CBI drops the leading zero of a New England EIN ("42103606"),
    and a hand-typed one may carry the IRS hyphen ("04-2103606"). Padding stops at one zero in
    effect: the IRS never issues a "00" prefix, so anything shorter than eight digits is not an
    EIN that lost its zero but a typo."""
    digits = re.sub(r"\D", "", text or "").zfill(9)
    return digits if len(digits) == 9 and not digits.startswith("00") else None


def normalise_name(text: str | None) -> str:
    """Upper case, punctuation out, the common words out, one space between the rest: "Mount
    Auburn Hospital, Inc." and "MOUNT AUBURN HOSPITAL" both become "MOUNT AUBURN"."""
    words = re.sub(r"[^A-Z0-9]+", " ", (text or "").upper()).split()
    return " ".join(word for word in words if word not in COMMON_WORDS)


def _ccn(text: str | None) -> str:
    """A CCN as the registry spells it: six characters, so a source that lost the leading zero
    of "010001" is padded back."""
    value = (text or "").strip()
    return value.zfill(6) if value.isdigit() and len(value) < 6 else value


def _zip5(text: str | None) -> str:
    """The five-digit ZIP, with the leading zero put back when a source dropped it (a ZIP+4
    with a lost zero is eight digits)."""
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return ""
    return digits.zfill(9)[:5] if len(digits) > 5 else digits.zfill(5)


def _fit(text: str | None, column: str) -> str | None:
    """`text` cut to what the hospital_ids column holds. The brief's npi_vintage ("Hospital
    Enrollments, 2026-08-01 to 2026-08-31, file of 2026-08-17") is 66 characters and the column
    is String(60): SQLite stores it anyway, PostgreSQL refuses the whole upsert. Losing the tail
    is the smaller harm until tables.py widens the column; once it does, nothing is cut."""
    if text is None:
        return None
    limit = HospitalIdRow.__table__.c[column].type.length
    if limit is None or len(text) <= limit:
        return text
    log.warning("hospital_ids.%s holds %d characters; cutting %r", column, limit, text)
    return text[:limit].rstrip(" ,-")


def _cbi_vintage(row: dict[str, Any], fallback: str | None) -> str:
    """ "Community Benefit Insight, updated 2024-12-06" from the row's updated_dt ("December 6,
    2024"), the raw text when it does not parse, the caller's fallback when there is none."""
    raw = str(row.get("updated_dt") or "").strip()
    if not raw:
        return fallback or CBI_SOURCE
    try:
        stamp = datetime.strptime(raw, "%B %d, %Y").date().isoformat()
    except ValueError:
        stamp = raw
    return f"{CBI_SOURCE}, updated {stamp}"


def fetch_cbi(
    http: httpx.Client, state: str, cache_dir: Path, refresh: bool = False
) -> list[dict[str, Any]]:
    """Community Benefit Insight's hospitals for one state, cached as `<cache_dir>/cbi/XX.json`."""
    state = state.upper()
    path = download.fetch_file(
        http, CBI_URL.format(state=state), cache_dir / "cbi", name=f"{state}.json", refresh=refresh
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = next((value for value in data.values() if isinstance(value, list)), [])
    return [row for row in data if isinstance(row, dict)]


def _decode(raw: bytes) -> str:
    """CMS ships Hospital Enrollments in cp1252 (a stray "¿" in an address breaks utf-8), with
    no promise it stays that way; utf-8 is tried first so a BOM-prefixed file reads too."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def fetch_enrollments(
    http: httpx.Client, cache_dir: Path, catalog: list[dict[str, Any]], refresh: bool = False
) -> tuple[list[dict[str, Any]], str]:
    """The newest Hospital Enrollments CSV in the CMS catalogue, as rows, with its vintage."""
    ref = download.latest_dataset(catalog, ENROLLMENTS_TITLE)
    if ref is None or not ref.csv_url:
        raise ValueError(f"{ENROLLMENTS_TITLE} is not in the CMS catalogue")
    path = download.fetch_file(http, ref.csv_url, cache_dir / "enrollments", refresh=refresh)
    rows = list(csv.DictReader(io.StringIO(_decode(path.read_bytes()), newline="")))
    return rows, download.vintage_of(ref)


@dataclass(frozen=True)
class Candidate:
    """A CBI row that might be an unmatched hospital, with how alike the two names are."""

    ccn: str
    name: str
    ein: str
    score: float


@dataclass
class CrosswalkReport:
    """What one state's pass did: counts per method and the hospitals left for the founder."""

    state: str
    hospitals: int = 0
    matched: int = 0
    unmatched: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    unmatched_rows: list[dict[str, Any]] = field(default_factory=list)


def _usable_cbi(cbi_rows: list[dict[str, Any]], state: str) -> list[dict[str, Any]]:
    """The CBI rows of this state that carry a nine-digit EIN; the rest cannot match anything."""
    return [
        row
        for row in cbi_rows
        if normalise_ein(str(row.get("ein") or ""))
        and str(row.get("state") or state).upper() == state
    ]


def _cbi_by_ccn(cbi: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The usable CBI row per CCN. The first row wins when a CCN repeats (none does in the MA
    file, but nothing promises that for other states), and the repeat is logged so a wrong
    pick shows up in the run's output rather than silently in a hospital's EIN."""
    by_ccn: dict[str, dict[str, Any]] = {}
    for row in cbi:
        ccn = _ccn(row.get("medicare_provider_number"))
        if not ccn:
            continue
        if ccn in by_ccn:
            # Names, not EINs: the redacting filter drops a record with seven digits in a row.
            log.warning(
                "CBI names CCN %s twice: keeping %r, skipping %r",
                ccn,
                by_ccn[ccn].get("name"),
                row.get("name"),
            )
            continue
        by_ccn[ccn] = row
    return by_ccn


def _enrollment_for(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The row that stands for the hospital among its practice locations: the MAIN one, else
    the first."""
    for row in rows:
        if str(row.get("PRACTICE LOCATION TYPE") or "").upper().startswith("MAIN"):
            return row
    return rows[0] if rows else None


def _enrolled_rows(enrollments: dict[str, list[dict[str, Any]]], ccn: str) -> list[dict[str, Any]]:
    """The rows enrolled under this CCN, or, when there are none, under a unit of it: a few
    hospitals (BID Needham, 220083) appear only as "220083A", the same legal entity with the
    same associate id and nonprofit flag, and that NPI is the best the file offers."""
    rows = enrollments.get(ccn)
    if rows:
        return rows
    return [
        row for suffix in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for row in enrollments.get(ccn + suffix, [])
    ]


def _best_candidate(name: str, cbi_rows: list[dict[str, Any]]) -> Candidate | None:
    """The CBI row whose normalised name is most like `name`, whatever its city."""
    best: Candidate | None = None
    if not name:
        return None
    for row in cbi_rows:
        other = normalise_name(row.get("name"))
        if not other:
            continue
        score = round(fuzz.token_set_ratio(name, other), 1)
        if best is None or score > best.score:
            best = Candidate(
                _ccn(row.get("medicare_provider_number")),
                str(row.get("name") or ""),
                normalise_ein(str(row.get("ein") or "")) or "",
                score,
            )
    return best


def crosswalk_state(
    session: Session,
    state: str,
    cbi_rows: list[dict[str, Any]],
    enrollment_rows: list[dict[str, Any]],
    today: date,
    *,
    cbi_vintage: str | None = None,
    enrollments_vintage: str = "",
) -> CrosswalkReport:
    """Tie every registry hospital of `state` to its NPI (always, when enrolled) and its EIN
    (by the best method that finds one), and say what happened. Two passes: CBI-by-CCN matches
    first, so a sibling enrolled by the same legal entity can inherit in the second."""
    state = state.upper()
    report = CrosswalkReport(state=state)
    cbi = _usable_cbi(cbi_rows, state)
    cbi_by_ccn = _cbi_by_ccn(cbi)
    enrollments: dict[str, list[dict[str, Any]]] = {}
    for row in enrollment_rows:
        enrollments.setdefault(_ccn(row.get("CCN")), []).append(row)
    npi_vintage = _fit(f"{ENROLLMENTS_TITLE}, {enrollments_vintage}", "npi_vintage")

    hospitals = [
        h for h in atlas_repo.list_hospitals(session, state) if not atlas_repo.is_demo(h.ccn)
    ]
    report.hospitals = len(hospitals)
    in_this_run = {h.ccn for h in hospitals}

    # What a sibling can inherit: associate id -> (ein, matched_name, ein_vintage), from the CBI
    # matches stored for hospitals outside this run, overwritten by this run's own. A stored
    # row of this state is stale by definition (the pass is about to redo it), so a hospital
    # whose CBI row vanished never inherits its own old EIN, and a sibling follows the fresh
    # match rather than last time's.
    by_associate: dict[str, tuple[str, str | None, str | None]] = {
        row.associate_id: (row.ein, row.matched_name, row.ein_vintage)
        for row in ledger_repo.list_hospital_ids(session)
        if row.match_method == "cbi" and row.ein and row.associate_id and row.ccn not in in_this_run
    }

    pending: list[tuple[HospitalRow, HospitalIdRow]] = []
    for hospital in hospitals:
        fields: dict[str, Any] = {"checked_on": today}
        enrolled = _enrollment_for(_enrolled_rows(enrollments, hospital.ccn))
        if enrolled is not None:
            fields.update(
                npi=str(enrolled.get("NPI") or "").strip() or None,
                associate_id=str(enrolled.get("ASSOCIATE ID") or "").strip() or None,
                nonprofit_flag=str(enrolled.get("PROPRIETARY NONPROFIT") or "").strip()[:1] or None,
                npi_vintage=npi_vintage,
            )
        row = ledger_repo.upsert_hospital_id(session, hospital.ccn, **fields)
        if row.match_method == "manual" and row.ein:
            _count(report, "manual")
            continue
        found = cbi_by_ccn.get(hospital.ccn)
        if found is not None:
            _match(row, "cbi", found, cbi_vintage)
            _count(report, "cbi")
            if row.associate_id:
                by_associate[row.associate_id] = (row.ein, row.matched_name, row.ein_vintage)
            continue
        pending.append((hospital, row))

    for hospital, row in pending:
        inherited = by_associate.get(row.associate_id or "")
        if inherited is not None:
            row.ein, row.matched_name, row.ein_vintage = inherited
            row.match_method, row.match_confidence = "enrollments", CONFIDENCE["enrollments"]
            _count(report, "enrollments")
            continue
        name = normalise_name(hospital.name)
        zip5 = _zip5(hospital.zip)
        # Both name steps need a ZIP on the hospital's side: two blank ZIPs are not the same ZIP.
        same_zip = [c for c in cbi if zip5 and _zip5(c.get("zip_code")) == zip5]
        same_name_zip = next(
            (c for c in same_zip if name and normalise_name(c.get("name")) == name), None
        )
        if same_name_zip is not None:
            _match(row, "name_zip", same_name_zip, cbi_vintage)
            _count(report, "name_zip")
            continue
        city = (hospital.city or "").strip().lower()
        nearby = [c for c in same_zip if str(c.get("city") or "").strip().lower() == city]
        best = _best_candidate(name, nearby)
        if best is not None and best.score >= FUZZY_THRESHOLD:
            fuzzy_row = next(c for c in nearby if str(c.get("name") or "") == best.name)
            _match(row, "fuzzy", fuzzy_row, cbi_vintage)
            _count(report, "fuzzy")
            continue
        row.ein = row.match_method = row.match_confidence = None
        row.matched_name = row.ein_vintage = None
        anywhere = _best_candidate(name, cbi)
        report.unmatched_rows.append(
            {
                "ccn": hospital.ccn,
                "name": hospital.name,
                "city": hospital.city,
                "zip": hospital.zip,
                "best_name": anywhere.name if anywhere else "",
                "best_ein": anywhere.ein if anywhere else "",
                "best_score": anywhere.score if anywhere else 0,
            }
        )
    session.flush()
    report.matched = sum(report.by_method.values())
    report.unmatched = report.hospitals - report.matched
    log.info(
        "crosswalk %s: %d hospitals, %d matched %s, %d unmatched",
        state,
        report.hospitals,
        report.matched,
        report.by_method,
        report.unmatched,
    )
    return report


def _match(row: HospitalIdRow, method: str, cbi_row: dict[str, Any], fallback: str | None) -> None:
    row.ein = normalise_ein(str(cbi_row.get("ein") or ""))
    row.match_method = method
    row.match_confidence = CONFIDENCE[method]
    row.matched_name = _fit(str(cbi_row.get("name") or "").strip() or None, "matched_name")
    row.ein_vintage = _fit(_cbi_vintage(cbi_row, fallback), "ein_vintage")


def _count(report: CrosswalkReport, method: str) -> None:
    report.by_method[method] = report.by_method.get(method, 0) + 1


def set_manual(session: Session, ccn: str, ein: str, today: date) -> HospitalIdRow:
    """Record an EIN the founder resolved by hand; later passes keep it."""
    clean = normalise_ein(ein)
    if clean is None:
        raise ValueError(f"not a nine-digit EIN: {ein!r}")
    if atlas_repo.is_demo(ccn):
        raise ValueError(f"CCN {ccn} is the demo hospital, which has no ledger")
    if session.get(HospitalRow, ccn) is None:
        raise ValueError(f"no registry hospital with CCN {ccn}")
    return ledger_repo.upsert_hospital_id(
        session,
        ccn,
        ein=clean,
        match_method="manual",
        match_confidence=CONFIDENCE["manual"],
        ein_vintage=f"set by hand, {today.isoformat()}",
        checked_on=today,
    )


def write_unmatched(report: CrosswalkReport, path: Path) -> Path:
    """The hospitals a pass could not tie to an EIN, with the nearest CBI name, as a CSV the
    founder resolves by hand."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=UNMATCHED_COLUMNS)
        writer.writeheader()
        writer.writerows(report.unmatched_rows)
    return path


def run_crosswalk(
    session: Session,
    http: httpx.Client,
    state: str,
    cache_dir: Path,
    catalog: list[dict[str, Any]],
    today: date,
    refresh: bool = False,
) -> CrosswalkReport:
    """Fetch both files (cached) and run one state's pass."""
    cbi_rows = fetch_cbi(http, state, cache_dir, refresh=refresh)
    enrollment_rows, vintage = fetch_enrollments(http, cache_dir, catalog, refresh=refresh)
    return crosswalk_state(
        session, state, cbi_rows, enrollment_rows, today, enrollments_vintage=vintage
    )
