"""IRS Form 990 Schedule H for the ledger (Phase 10): the e-file index, the bulk ZIPs and the
facts a hospital states about its own financial assistance and collections.

The IRS publishes every e-filed 990 as XML, one index CSV per year and one 100-300 MB ZIP per
batch of about 40,000 returns. Waive wants a few hundred of those returns, so the loader reads
the index to learn which batch holds each matched EIN's newest 990, downloads each batch once,
pulls only the members it needs, and keeps the small XML files under `var/ledger/irs990/xml/`.
Schedule H Part I gives financial assistance at cost as a share of expense; Part V Section B
gives, per facility, the free-care income limit and which collection actions the facility's
policy permits (line 18) and which it took before checking eligibility (line 19). A return
covers every facility of the filer, so the facts are matched back to each hospital by name,
state and ZIP. Public data only: no model calls, no credits."""

from __future__ import annotations

import csv
import logging
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from rapidfuzz import fuzz
from sqlalchemy import delete, or_
from sqlalchemy.orm import Session

from waive.atlas import repo as atlas_repo
from waive.ledger import repo
from waive.ledger.download import fetch_file
from waive.ledger.tables import ScheduleHRow

log = logging.getLogger(__name__)

INDEX_URL = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv"
BATCH_URL = "https://apps.irs.gov/pub/epostcard/990/xml/{year}/{batch_id}.zip"
IRS_NS = "http://www.irs.gov/efile"

# Only the full Form 990 carries Schedule H; 990-EZ, 990-PF and 990-T never do.
RETURN_TYPE = "990"
# The name token_set_ratio must reach before a facility counts as the hospital.
NAME_THRESHOLD = 80
# Schemas through 2013v4.0 ask about lawsuits, liens and body attachments on separate lines and
# never about selling debt; 2014v5.0 to 2015v3.0 ask about selling debt and nothing about a
# legal process; from 2016v3.0 one line covers "actions that require a legal or judicial
# process" (the concordance's versions column for each element).
SEPARATE_LEGAL_SCHEMA_YEAR = 2013
FUSED_LEGAL_SCHEMA_YEAR = 2016
# Words that say what kind of company a filer is, not which hospital: dropped before matching.
_LEGAL_WORDS = frozenset(
    {"inc", "llc", "corp", "corporation", "co", "the", "of", "and", "dba", "ltd"}
)
_CHECKED = frozenset({"x", "true", "1"})


@dataclass(frozen=True)
class IndexEntry:
    """One row of the e-file index: which batch ZIP holds which return."""

    ein: str
    tax_period: str
    return_type: str
    object_id: str
    batch_id: str
    index_year: int
    sub_date: str
    taxpayer_name: str


@dataclass(frozen=True)
class FacilityFacts:
    """Part V for one hospital facility: who it is (Section A) and what its policy says and
    its collectors did (Section B). Checkbox fields are None only when the facility has no
    Section B group or the schema did not ask; inside a group an absent box means unchecked."""

    line: int | None
    name: str | None
    city: str | None
    state: str | None
    zip5: str | None
    license: str | None
    free_care_fpl_pct: float | None
    discount_care_fpl_pct: float | None
    asset_test: bool | None
    credit_reporting: bool | None
    sells_debt: bool | None
    legal_action: bool | None
    lawsuits: bool | None
    liens: bool | None
    other_actions: bool | None
    # "engaged" when the facility has a Section B group (its flags are line 19 answers).
    flag_line: str | None
    # Line 18, what the policy permits: credit_reporting, selling_debt, legal_action, other, none.
    permitted_actions: tuple[str, ...]


@dataclass(frozen=True)
class ReturnFacts:
    """What one return says: the header, Part I line 7a and every facility of Part V."""

    ein: str | None
    tax_period: str | None
    tax_year: int
    period_end: date | None
    filer_name: str | None
    return_version: str
    charity_pct_of_expense: float | None
    facilities: tuple[FacilityFacts, ...]


@dataclass
class Irs990Report:
    """What one run did, for the CLI's summary line."""

    eins_wanted: int = 0
    returns_found: int = 0
    batches_downloaded: int = 0
    xml_fetched: int = 0
    hospitals_loaded: int = 0
    facilities_unmatched: list[dict[str, Any]] = field(default_factory=list)
    # Selected returns whose member the batch ZIP did not hold (ein, object_id, batch_id).
    returns_missing: list[dict[str, Any]] = field(default_factory=list)
    # Hospitals matched to a Section A facility the return answered no Section B group for,
    # although it answered for others: the row carries the return-level facts only.
    facilities_unanswered: list[dict[str, Any]] = field(default_factory=list)


# --- the index -----------------------------------------------------------------------------


def normalise_ein(raw: str | None) -> str | None:
    """Nine digits: the index drops leading zeros ("42103606" for 04-2103606)."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits or len(digits) > 9:
        return None
    return digits.zfill(9)


def fetch_index(http: httpx.Client, cache_dir: Path, year: int, refresh: bool = False) -> Path:
    """The year's index CSV, cached as `cache_dir/irs990/index_{year}.csv`."""
    return fetch_file(http, INDEX_URL.format(year=year), cache_dir / "irs990", refresh=refresh)


def _index_year(batch_id: str, path: Path) -> int:
    """The year directory the batch ZIP sits in: the batch id starts with it ("2025_TEOS_XML_08A");
    the index file's name is the fallback."""
    if batch_id[:4].isdigit():
        return int(batch_id[:4])
    found = re.search(r"(\d{4})", path.stem)
    return int(found.group(1)) if found else 0


def _recency(entry: IndexEntry) -> tuple[str, str, str]:
    # A later submission of the same period (an amended return) has a larger object id.
    return (entry.tax_period, entry.sub_date, entry.object_id)


def select_returns(index_paths: list[Path], eins: set[str]) -> dict[str, IndexEntry]:
    """Each wanted EIN's newest full 990 across the index files, streamed row by row because an
    index is 90 MB. ein -> entry."""
    chosen: dict[str, IndexEntry] = {}
    for path in index_paths:
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            for row in csv.DictReader(handle):
                if (row.get("RETURN_TYPE") or "").strip() != RETURN_TYPE:
                    continue
                ein = normalise_ein(row.get("EIN"))
                if ein is None or ein not in eins:
                    continue
                batch_id = (row.get("XML_BATCH_ID") or "").strip()
                entry = IndexEntry(
                    ein=ein,
                    tax_period=(row.get("TAX_PERIOD") or "").strip(),
                    return_type=RETURN_TYPE,
                    object_id=(row.get("OBJECT_ID") or "").strip(),
                    batch_id=batch_id,
                    index_year=_index_year(batch_id, path),
                    sub_date=(row.get("SUB_DATE") or "").strip(),
                    taxpayer_name=(row.get("TAXPAYER_NAME") or "").strip(),
                )
                current = chosen.get(ein)
                if current is None or _recency(entry) > _recency(current):
                    chosen[ein] = entry
    return chosen


# --- the bulk ZIPs -------------------------------------------------------------------------


def _member(object_id: str) -> str:
    return f"{object_id}_public.xml"


def _xml_dir(cache_dir: Path) -> Path:
    return cache_dir / "irs990" / "xml"


def _zip_dir(cache_dir: Path) -> Path:
    return cache_dir / "irs990" / "zips"


# Some IRS batches (2026_TEOS_XML_05A) are written with Deflate64, which Python's zipfile
# does not read; Info-ZIP's `unzip` does. Member names are checked against this before one is
# handed to it.
MEMBER_NAME = re.compile(r"^\d{18}_public\.xml$")
UNZIP_TIMEOUT = 600


def _extract_with_unzip(zip_path: str, member: str, out) -> None:
    if not MEMBER_NAME.match(member):
        raise ValueError(f"not a return member name: {member!r}")
    try:
        subprocess.run(
            ["unzip", "-p", zip_path, member],
            stdout=out,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=UNZIP_TIMEOUT,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            f"{Path(zip_path).name} uses a compression Python cannot read and `unzip` is not "
            "installed; install Info-ZIP unzip and run again"
        ) from error


def _extract(archive: zipfile.ZipFile, member: str, target: Path) -> Path:
    """One member to `target` through a `.part` file, so an interrupted run leaves no half
    XML under the real name."""
    partial = target.with_name(target.name + ".part")
    try:
        with partial.open("wb") as out:
            try:
                with archive.open(member) as source:
                    shutil.copyfileobj(source, out)
            except NotImplementedError:
                # Deflate64: let the system's unzip write the member instead.
                _extract_with_unzip(str(archive.filename), member, out)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def extract_members(zip_path: Path, object_ids: Iterable[str], xml_dir: Path) -> dict[str, Path]:
    """The returns with these object ids out of one batch ZIP, opened once for all of them.
    Members already under `xml_dir` are kept; ids the ZIP lacks are logged and left out."""
    found: dict[str, Path] = {}
    wanted: dict[str, str] = {}
    for object_id in object_ids:
        target = xml_dir / _member(object_id)
        if target.exists():
            found[object_id] = target
        else:
            wanted[_member(object_id)] = object_id
    if not wanted:
        return found
    xml_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        for member, object_id in wanted.items():
            if member not in names:
                log.warning("%s is not in %s", member, zip_path.name)
                continue
            found[object_id] = _extract(archive, member, xml_dir / member)
    return found


def fetch_return_xml(
    http: httpx.Client, cache_dir: Path, entry: IndexEntry, refresh: bool = False
) -> Path:
    """One return's XML, cached as `cache_dir/irs990/xml/{object_id}_public.xml`; the batch ZIP
    is downloaded (once) when the member is missing. KeyError when the ZIP lacks the member."""
    target = _xml_dir(cache_dir) / _member(entry.object_id)
    if target.exists() and not refresh:
        return target
    zip_path = fetch_file(
        http, BATCH_URL.format(year=entry.index_year, batch_id=entry.batch_id), _zip_dir(cache_dir)
    )
    _xml_dir(cache_dir).mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        member = _member(entry.object_id)
        if member not in archive.namelist():
            raise KeyError(f"{member} is not in {zip_path.name}")
        return _extract(archive, member, target)


# --- Schedule H ----------------------------------------------------------------------------


def _q(tag: str) -> str:
    return f"{{{IRS_NS}}}{tag}"


def _text(element: ET.Element | None, *tags: str) -> str | None:
    """The stripped text at `tags` below `element`, walking one namespaced child per tag."""
    for tag in tags:
        if element is None:
            return None
        element = element.find(_q(tag))
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _first_text(element: ET.Element | None, *paths: tuple[str, ...]) -> str | None:
    for path in paths:
        value = _text(element, *path)
        if value is not None:
            return value
    return None


def _float(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text[:10]) if text else None
    except ValueError:
        return None


def _percent(text: str | None) -> float | None:
    """Line 7a is a fraction of total expense in the file ("0.02610"); the ledger keeps percents."""
    value = _float(text)
    return round(value * 100, 6) if value is not None else None


def _checked(group: ET.Element, tag: str) -> bool:
    """A checkbox inside a group that exists: present with X (or true/1) when checked, absent
    when not."""
    text = _text(group, tag)
    return text is not None and text.lower() in _CHECKED


def _schema_year(version: str) -> int:
    found = re.match(r"(\d{4})", version or "")
    return int(found.group(1)) if found else 9999


def _tax_year_of(period_end: date | None) -> int | None:
    """The form year is the year the period began: a period ending in September 2024 is tax
    year 2023, one ending in December 2024 is 2024."""
    if period_end is None:
        return None
    return period_end.year if period_end.month == 12 else period_end.year - 1


@dataclass(frozen=True)
class _Identity:
    """Part V Section A: one line per facility, with the facility reporting group (column 11)
    a filer that answers Section B once per group puts it in."""

    line: int | None
    name: str | None
    city: str | None
    state: str | None
    zip5: str | None
    license: str | None
    group: str | None


@dataclass(frozen=True)
class _Policy:
    """Part V Section B: one group per facility or facility reporting group."""

    line: int | None
    name: str | None
    free_care_fpl_pct: float | None
    discount_care_fpl_pct: float | None
    asset_test: bool | None
    credit_reporting: bool | None
    sells_debt: bool | None
    legal_action: bool | None
    lawsuits: bool | None
    liens: bool | None
    other_actions: bool | None
    permitted_actions: tuple[str, ...]

    def answers(self) -> tuple[Any, ...]:
        """The answers without the line and name: equal across groups when a system filled the
        same policy in for every facility."""
        return (
            self.free_care_fpl_pct,
            self.discount_care_fpl_pct,
            self.asset_test,
            self.credit_reporting,
            self.sells_debt,
            self.legal_action,
            self.lawsuits,
            self.liens,
            self.other_actions,
            self.permitted_actions,
        )


def _identity(group: ET.Element) -> _Identity:
    zip_code = _text(group, "USAddress", "ZIPCd")
    return _Identity(
        line=_int(_text(group, "FacilityNum")),
        name=_first_text(
            group, ("BusinessName", "BusinessNameLine1Txt"), ("BusinessName", "BusinessNameLine1")
        ),
        city=_text(group, "USAddress", "CityNm"),
        state=_text(group, "USAddress", "StateAbbreviationCd"),
        zip5=zip_code[:5] if zip_code else None,
        license=_text(group, "StateLicenseNum"),
        group=(_text(group, "FacilityReportingGroupCd") or "").upper() or None,
    )


def _policy(group: ET.Element, schema_year: int) -> _Policy:
    lawsuits: bool | None
    liens: bool | None
    legal_action: bool | None
    sells_debt: bool | None
    if schema_year >= FUSED_LEGAL_SCHEMA_YEAR:
        lawsuits = liens = None
        legal_action = _checked(group, "EngagedLegalJudicialProcessInd")
        sells_debt = _checked(group, "EngagedSellingDebtInd")
    elif schema_year > SEPARATE_LEGAL_SCHEMA_YEAR:
        # 2014 and 2015 schemas asked nothing about a legal process: unknown, not "no".
        lawsuits = liens = legal_action = None
        sells_debt = _checked(group, "EngagedSellingDebtInd")
    else:
        lawsuits = _checked(group, "LawsuitInd")
        liens = _checked(group, "LiensOnResidencesInd")
        legal_action = lawsuits or liens or _checked(group, "BodyAttachmentsInd")
        sells_debt = None
    permitted: list[str] = []
    if _checked(group, "PermitReportToCreditAgencyInd"):
        permitted.append("credit_reporting")
    if _checked(group, "PermitSellingDebtInd"):
        permitted.append("selling_debt")
    if any(
        _checked(group, tag)
        for tag in (
            "PermitLegalJudicialProcessInd",
            "PermitLawsuitInd",
            "PermitLienOnResidenceInd",
            "PermitBodyAttachmentsInd",
        )
    ):
        permitted.append("legal_action")
    if _checked(group, "PermitOtherActionsInd"):
        permitted.append("other")
    if _checked(group, "PermitNoActionsInd"):
        permitted.append("none")
    return _Policy(
        line=_int(_text(group, "FacilityNum")),
        name=_first_text(
            group,
            ("HospitalFacilityName", "BusinessNameLine1Txt"),
            ("HospitalFacilityName", "BusinessNameLine1"),
        ),
        free_care_fpl_pct=_float(_text(group, "FPGFamilyIncmLmtFreeCarePct")),
        discount_care_fpl_pct=_float(_text(group, "FPGFamilyIncmLmtDscntCarePct")),
        asset_test=_checked(group, "AssetLevelCriteriaInd") or _checked(group, "AssetLevelInd"),
        credit_reporting=_checked(group, "ReportingToCreditAgencyInd"),
        sells_debt=sells_debt,
        legal_action=legal_action,
        lawsuits=lawsuits,
        liens=liens,
        other_actions=_checked(group, "OtherActionsTakenInd"),
        permitted_actions=tuple(permitted),
    )


def _combine(identity: _Identity | None, policy: _Policy | None) -> FacilityFacts:
    return FacilityFacts(
        line=identity.line if identity else (policy.line if policy else None),
        name=(identity.name if identity else None) or (policy.name if policy else None),
        city=identity.city if identity else None,
        state=identity.state if identity else None,
        zip5=identity.zip5 if identity else None,
        license=identity.license if identity else None,
        free_care_fpl_pct=policy.free_care_fpl_pct if policy else None,
        discount_care_fpl_pct=policy.discount_care_fpl_pct if policy else None,
        asset_test=policy.asset_test if policy else None,
        credit_reporting=policy.credit_reporting if policy else None,
        sells_debt=policy.sells_debt if policy else None,
        legal_action=policy.legal_action if policy else None,
        lawsuits=policy.lawsuits if policy else None,
        liens=policy.liens if policy else None,
        other_actions=policy.other_actions if policy else None,
        flag_line="engaged" if policy else None,
        permitted_actions=policy.permitted_actions if policy else (),
    )


def _group_code(name: str | None) -> str | None:
    """The reporting group a Section B name stands for: filers write the group's code alone
    ("A", "1") or spelled out ("FACILITY REPORTING GROUP - A"); a facility's own name is not
    a code."""
    if not name:
        return None
    found = re.fullmatch(r"\s*([A-Za-z0-9])\s*", name) or re.search(
        r"\bgroup\s*-?\s*([A-Za-z0-9])\s*$", name, re.IGNORECASE
    )
    return found.group(1).upper() if found else None


def _distinct_lines(policies: list[_Policy]) -> dict[int, _Policy]:
    """line -> group when every group carries its own FacilityNum; empty otherwise."""
    lines = [policy.line for policy in policies]
    if None in lines or len(set(lines)) != len(lines):
        return {}
    return {policy.line: policy for policy in policies if policy.line is not None}


def _join(identities: list[_Identity], policies: list[_Policy]) -> list[_Policy | None] | None:
    """Section B groups joined to Section A facilities by what the filer wrote in the group:
    the reporting group's code reaches every facility in that group (WakeMed answers once,
    on FacilityNum 2 named "A", for two facilities both in group A), the facility's own name
    reaches that facility (Maria Parham numbers its only facility 5 in Section B), and a lone
    group reaches every facility when they are all in one reporting group. A facility left
    over takes the group with its own FacilityNum. None when nothing joined."""
    by_group: dict[str, _Policy] = {}
    by_name: dict[str, _Policy] = {}
    for policy in policies:
        code = _group_code(policy.name)
        if code:
            by_group.setdefault(code, policy)
        elif policy.name:
            by_name.setdefault(_normal_name(policy.name), policy)
    chosen: list[_Policy | None] = []
    for identity in identities:
        policy = by_group.get(identity.group) if identity.group else None
        if policy is None and identity.name:
            policy = by_name.get(_normal_name(identity.name))
        chosen.append(policy)
    groups = {identity.group for identity in identities}
    if len(policies) == 1 and len(groups) == 1 and None not in groups:
        return [policies[0]] * len(identities)
    if not any(chosen):
        return None
    by_line = _distinct_lines(policies)
    return [
        policy if policy is not None else by_line.get(identity.line)
        for policy, identity in zip(chosen, identities, strict=True)
    ]


def _attribute(identities: list[_Identity], policies: list[_Policy]) -> tuple[FacilityFacts, ...]:
    """Section B groups joined to Section A facilities: by reporting group or name first
    (`_join`). Otherwise filers number the groups like the facilities when they answer per
    facility; a system that answers once for everyone repeats the same group with FacilityNum 1
    (UMass Memorial lists four facilities that way), so equal answers apply to every facility,
    equal counts map in order, and anything else keeps only the first group, unattributed,
    rather than guessing."""
    if not policies:
        return tuple(_combine(identity, None) for identity in identities)
    joined = _join(identities, policies)
    if joined is not None:
        return tuple(_combine(i, p) for i, p in zip(identities, joined, strict=True))
    by_line = _distinct_lines(policies)
    if by_line:
        known = {identity.line for identity in identities}
        facilities = [_combine(identity, by_line.get(identity.line)) for identity in identities]
        facilities += [_combine(None, policy) for policy in policies if policy.line not in known]
        return tuple(facilities)
    if identities and len({policy.answers() for policy in policies}) == 1:
        return tuple(_combine(identity, policies[0]) for identity in identities)
    if identities and len(identities) == len(policies):
        return tuple(_combine(i, p) for i, p in zip(identities, policies, strict=True))
    return (_combine(None, replace(policies[0], line=None)),)


def parse_schedule_h(xml_bytes: bytes) -> ReturnFacts:
    """The header, Part I line 7a and Part V of one return; a return without Schedule H keeps
    the header only (no facilities, charity None). ElementTree with namespaced tags: a return
    is half a megabyte, and the paths are fixed by the schema."""
    root = ET.fromstring(xml_bytes)
    version = root.get("returnVersion") or ""
    header = root.find(_q("ReturnHeader"))
    period_end = _date(_text(header, "TaxPeriodEndDt"))
    tax_year = _int(_text(header, "TaxYr")) or _tax_year_of(period_end) or 0
    filer_name = _first_text(
        header,
        ("Filer", "BusinessName", "BusinessNameLine1Txt"),
        ("Filer", "BusinessName", "BusinessNameLine1"),
    )
    facts = ReturnFacts(
        ein=normalise_ein(_text(header, "Filer", "EIN")),
        tax_period=period_end.strftime("%Y%m") if period_end else None,
        tax_year=tax_year,
        period_end=period_end,
        filer_name=filer_name,
        return_version=version,
        charity_pct_of_expense=None,
        facilities=(),
    )
    schedule = root.find(f"{_q('ReturnData')}/{_q('IRS990ScheduleH')}")
    if schedule is None:
        return facts
    schema_year = _schema_year(version)
    identities = [_identity(g) for g in schedule.findall(_q("HospitalFacilitiesGrp"))]
    policies = [
        _policy(g, schema_year) for g in schedule.findall(_q("HospitalFcltyPoliciesPrctcGrp"))
    ]
    return replace(
        facts,
        charity_pct_of_expense=_percent(
            _first_text(
                schedule,
                ("FinancialAssistanceAtCostTyp", "TotalExpensePct"),
                ("CharityCareAtCostGrp", "TotalExpensePct"),
            )
        ),
        facilities=_attribute(identities, policies),
    )


# --- matching ------------------------------------------------------------------------------


def _normal_name(name: str) -> str:
    text = name.lower().replace("st.", "saint").replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return " ".join(word for word in text.split() if word not in _LEGAL_WORDS)


def match_facility(
    facts: ReturnFacts, hospital_name: str, city: str | None, state: str, zip5: str | None
) -> FacilityFacts | None:
    """The facility of the return that is this hospital: the only one in its state, or the one
    in its state whose name is closest (at least NAME_THRESHOLD), the same ZIP or city breaking
    ties. None rather than a guess when nothing is close."""
    wanted_state = (state or "").upper()
    in_state = [f for f in facts.facilities if (f.state or "").upper() == wanted_state]
    if not in_state:
        return None
    if len(in_state) == 1:
        return in_state[0]
    wanted = _normal_name(hospital_name)
    best: FacilityFacts | None = None
    best_score = 0.0
    for facility in in_state:
        if not facility.name:
            continue
        ratio = fuzz.token_set_ratio(wanted, _normal_name(facility.name))
        if ratio < NAME_THRESHOLD:
            continue
        score = ratio
        if zip5 and facility.zip5 == zip5[:5]:
            score += 10
        if city and facility.city and facility.city.upper() == city.upper():
            score += 5
        if score > best_score:
            best, best_score = facility, score
    return best


# --- the loader ----------------------------------------------------------------------------


def _fetch_returns(
    http: httpx.Client, cache_dir: Path, entries: Iterable[IndexEntry], report: Irs990Report
) -> dict[str, Path]:
    """The XML of every entry, object_id -> path: cached members first, then one pass over
    each batch ZIP that holds the rest. An entry whose member the ZIP lacks goes to the
    report's `returns_missing`, so the loss shows in the summary."""
    xml_dir = _xml_dir(cache_dir)
    found: dict[str, Path] = {}
    by_batch: dict[tuple[int, str], list[str]] = defaultdict(list)
    entries = list(entries)
    for entry in entries:
        target = xml_dir / _member(entry.object_id)
        if target.exists():
            found[entry.object_id] = target
        else:
            by_batch[(entry.index_year, entry.batch_id)].append(entry.object_id)
    for (year, batch_id), object_ids in sorted(by_batch.items()):
        if not (_zip_dir(cache_dir) / f"{batch_id}.zip").exists():
            report.batches_downloaded += 1
        zip_path = fetch_file(
            http, BATCH_URL.format(year=year, batch_id=batch_id), _zip_dir(cache_dir)
        )
        extracted = extract_members(zip_path, object_ids, xml_dir)
        report.xml_fetched += len(extracted)
        found.update(extracted)
    for entry in entries:
        if entry.object_id not in found:
            report.returns_missing.append(
                {"ein": entry.ein, "object_id": entry.object_id, "batch_id": entry.batch_id}
            )
    return found


def _year_of_period(tax_period: str) -> int:
    """Tax year from the index's YYYYMM period when the XML header says nothing."""
    period_end = _date(f"{tax_period[:4]}-{tax_period[4:6]}-01") if len(tax_period) == 6 else None
    return _tax_year_of(period_end) or 0


def _store(
    session: Session,
    ccn: str,
    entry: IndexEntry,
    facts: ReturnFacts,
    facility: FacilityFacts | None,
    today: date,
) -> None:
    """One ledger_schedule_h row: the return-level facts for every hospital on the EIN, the
    facility's own answers only when a facility was matched. Every column is set so a rerun
    after a better match never keeps a stale answer."""
    fields: dict[str, Any] = {
        "ein": entry.ein,
        "tax_period": facts.tax_period or entry.tax_period,
        "object_id": entry.object_id,
        "filer_name": facts.filer_name or entry.taxpayer_name,
        "facility_name": None,
        "facility_line": None,
        "free_care_fpl_pct": None,
        "discount_care_fpl_pct": None,
        "asset_test": None,
        "lawsuits": None,
        "liens": None,
        "credit_reporting": None,
        "sells_debt": None,
        "legal_action": None,
        "flag_line": None,
        "permitted_actions": None,
        "charity_pct_of_expense": facts.charity_pct_of_expense,
        "vintage": f"IRS e-file index {entry.index_year}, filed {entry.sub_date}",
        "loaded_on": today,
    }
    if facility is not None:
        fields.update(
            facility_name=facility.name,
            facility_line=facility.line,
            free_care_fpl_pct=facility.free_care_fpl_pct,
            discount_care_fpl_pct=facility.discount_care_fpl_pct,
            asset_test=facility.asset_test,
            lawsuits=facility.lawsuits,
            liens=facility.liens,
            credit_reporting=facility.credit_reporting,
            sells_debt=facility.sells_debt,
            legal_action=facility.legal_action,
            flag_line=facility.flag_line,
            permitted_actions=",".join(facility.permitted_actions) or None,
        )
    tax_year = facts.tax_year or _year_of_period(entry.tax_period)
    repo.upsert_schedule_h(session, ccn, tax_year, **fields)
    # The crosswalk may re-match a hospital to another filer: that filer's newest return can
    # be an older tax year than the rows the former filer left, and the newest row is the one
    # read, so the former filer's rows go.
    session.execute(
        delete(ScheduleHRow).where(
            ScheduleHRow.ccn == ccn,
            or_(ScheduleHRow.ein.is_(None), ScheduleHRow.ein != entry.ein),
        )
    )


def _answered_for_others(facts: ReturnFacts, facility: FacilityFacts) -> bool:
    """The return answered Section B for some facility but not this one."""
    return facility.flag_line is None and any(f.flag_line for f in facts.facilities)


def load_schedule_h(
    session: Session,
    http: httpx.Client,
    cache_dir: Path,
    *,
    today: date,
    index_years: tuple[int, ...] = (2024, 2025, 2026),
    refresh: bool = False,
    state: str | None = None,
) -> Irs990Report:
    """Every matched hospital's newest 990 Schedule H into ledger_schedule_h. `refresh` re-reads
    the index files (new filings land there); batch ZIPs and return XML never change for a
    given id, so they are always served from the cache. The demo hospital is skipped."""
    report = Irs990Report()
    eins_by_ccn = {
        ccn: ein
        for ccn, ein in repo.matched_eins(session, state).items()
        if not atlas_repo.is_demo(ccn)
    }
    wanted = set(eins_by_ccn.values())
    report.eins_wanted = len(wanted)
    if not wanted:
        return report
    index_paths = [fetch_index(http, cache_dir, year, refresh=refresh) for year in index_years]
    entries = select_returns(index_paths, wanted)
    report.returns_found = len(entries)
    xml_paths = _fetch_returns(http, cache_dir, entries.values(), report)
    parsed: dict[str, ReturnFacts] = {}
    for ccn, ein in eins_by_ccn.items():
        entry = entries.get(ein)
        if entry is None:
            continue
        path = xml_paths.get(entry.object_id)
        if path is None:
            continue
        if entry.object_id not in parsed:
            parsed[entry.object_id] = parse_schedule_h(path.read_bytes())
        facts = parsed[entry.object_id]
        hospital = atlas_repo.get_hospital(session, ccn)
        if hospital is None:
            continue
        facility = match_facility(
            facts, hospital.name, hospital.city, hospital.state, (hospital.zip or "")[:5]
        )
        _store(session, ccn, entry, facts, facility, today)
        report.hospitals_loaded += 1
        if facility is None:
            report.facilities_unmatched.append(
                {
                    "ccn": ccn,
                    "ein": ein,
                    "name": hospital.name,
                    "object_id": entry.object_id,
                    "facilities": [f.name for f in facts.facilities if f.name],
                }
            )
        elif _answered_for_others(facts, facility):
            report.facilities_unanswered.append(
                {
                    "ccn": ccn,
                    "ein": ein,
                    "name": hospital.name,
                    "object_id": entry.object_id,
                    "facility": facility.name,
                }
            )
    log.info(
        "irs990: %d EINs, %d returns, %d batches downloaded, %d XML fetched, %d missing, "
        "%d hospitals, %d unmatched, %d unanswered",
        report.eins_wanted,
        report.returns_found,
        report.batches_downloaded,
        report.xml_fetched,
        len(report.returns_missing),
        report.hospitals_loaded,
        len(report.facilities_unmatched),
        len(report.facilities_unanswered),
    )
    return report
