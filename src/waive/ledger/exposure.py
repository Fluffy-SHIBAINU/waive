"""Who each hospital serves (Phase 10, metric 2's inputs): Medicare inpatient counts with the
dual-eligible share, and the poor seniors living in the ZIP codes the hospital draws from.

Three public files. CMS's "Medicare Inpatient Hospitals - by Provider" gives each
hospital's beneficiaries, discharges and how many beneficiaries were also on Medicaid (dual
eligible: the surest sign of a low-income patient). CMS's "Hospital Service Area" gives, per
hospital and ZIP of residence, how many Medicare cases came from that ZIP. The Census ACS table
B17024 gives, per ZIP code tabulation area, how many people 65 and over live under 200 % of the
poverty line. A hospital's poor seniors served is the ACS count of each ZIP it draws from, times
its share of that ZIP's unsuppressed Medicare cases, summed: a ZIP split between two hospitals
is split between them, never counted twice.

CMS suppresses a count under 11: blank in the inpatient file, "*" in the service area file.
A suppressed ZIP row is counted (so the page can say how much is hidden) but adds nothing.

The Census API is asked first (one request, every ZCTA). Since October 2026 it answers a keyless
request with a "Missing Key" page, so the same cells are then read from the table-based summary
file on www2.census.gov (240 MB, no key); either way the answer is cached once, in the API's
shape, as `acs/b17024_<year>.json`."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from waive.atlas.repo import is_demo, list_hospitals
from waive.ledger import download
from waive.ledger.repo import upsert_exposure

log = logging.getLogger(__name__)

INPATIENT_TITLE = "Medicare Inpatient Hospitals - by Provider"
SERVICE_AREA_TITLE = "Hospital Service Area"
INPATIENT_LANDING = (
    "https://data.cms.gov/provider-summary-by-type-of-service/medicare-inpatient-hospitals"
    "/medicare-inpatient-hospitals-by-provider"
)
SERVICE_AREA_LANDING = (
    "https://data.cms.gov/provider-summary-by-type-of-service/medicare-inpatient-hospitals"
    "/hospital-service-area"
)
ACS_LANDING = "https://data.census.gov/table/ACSDT5Y2024.B17024"
# B17024 is age by ratio of income to poverty. 107 to 114 are the eight bands under 2.00 for
# 65 to 74 years; 120 to 127 the same bands for 75 and over.
ACS_VARIABLES: tuple[str, ...] = tuple(
    f"B17024_{n:03d}E" for n in (*range(107, 115), *range(120, 128))
)
ACS_URL = (
    "https://api.census.gov/data/{year}/acs/acs5?get=NAME,{variables}"
    "&for=zip%20code%20tabulation%20area:*"
)
ACS_ZCTA_COLUMN = "zip code tabulation area"
# Where api.census.gov sends a request without a key.
ACS_MISSING_KEY = "https://api.census.gov/data/missing_key.html"
# The same table as one pipe-delimited file for every geography (tracts, places, ZCTAs, ...).
ACS_SUMMARY_URL = (
    "https://www2.census.gov/programs-surveys/acs/summary_file/{year}/table-based-SF/data"
    "/5YRData/acsdt5y{year}-b17024.dat"
)
# A ZCTA's GEO_ID in the summary file: 860Z200US followed by the five digits.
ACS_ZCTA_GEOID = "860Z200US"
SUPPRESSED = "*"


@dataclass(frozen=True)
class InpatientFacts:
    """One row of the inpatient file: None where CMS left a count blank."""

    ccn: str
    discharges: int | None
    benes: int | None
    dual_benes: int | None


@dataclass
class ServiceArea:
    """The service area file reduced to what the weighting needs: each wanted hospital's
    unsuppressed cases by ZIP, every ZIP's unsuppressed cases over every provider in the file
    (the denominator of a hospital's share), and how many ZIP rows CMS hid per hospital."""

    cases: dict[str, dict[str, int]] = field(default_factory=dict)
    zip_totals: dict[str, int] = field(default_factory=dict)
    suppressed: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ExposureFacts:
    """What goes into one `ledger_exposure` row."""

    ccn: str
    discharges: int | None
    benes: int | None
    dual_benes: int | None
    dual_discharges: float | None
    zips_counted: int
    zips_suppressed: int
    poor_seniors_served: float | None


@dataclass
class ExposureReport:
    """What `waive ledger exposure` prints: how many hospitals each file covered."""

    hospitals: int = 0
    with_inpatient: int = 0
    with_service_area: int = 0
    with_both: int = 0
    zips_matched_to_acs: int = 0
    acs_zctas: int = 0


def _count(text: str | None) -> int | None:
    """A CMS count cell as an int; None when blank or suppressed."""
    text = (text or "").strip()
    if not text or text == SUPPRESSED:
        return None
    return int(float(text))


def _zip5(text: str) -> str:
    """A ZIP as the five-character text the ACS keys on; a lost leading zero is put back."""
    text = text.strip()
    return text.zfill(5) if text.isdigit() and len(text) < 5 else text


def parse_inpatient_csv(path: Path) -> dict[str, InpatientFacts]:
    """Every provider in the inpatient file, keyed by CCN."""
    facts: dict[str, InpatientFacts] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ccn = (row.get("Rndrng_Prvdr_CCN") or "").strip()
            if not ccn:
                continue
            facts[ccn] = InpatientFacts(
                ccn=ccn,
                discharges=_count(row.get("Tot_Dschrgs")),
                benes=_count(row.get("Tot_Benes")),
                dual_benes=_count(row.get("Bene_Dual_Cnt")),
            )
    return facts


def parse_service_area_csv(path: Path, ccns: set[str]) -> ServiceArea:
    """The service area file, streamed row by row (1.2 million rows): the wanted hospitals'
    ZIPs, and every ZIP's total so a hospital's share can be taken."""
    area = ServiceArea()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = [name.strip() for name in next(reader, [])]
        try:
            provider_at = header.index("MEDICARE_PROV_NUM")
            zip_at = header.index("ZIP_CD_OF_RESIDENCE")
            cases_at = header.index("TOTAL_CASES")
        except ValueError as error:
            raise ValueError(f"{path.name} lacks the service area columns: {header}") from error
        width = max(provider_at, zip_at, cases_at) + 1
        for row in reader:
            if len(row) < width:
                continue
            provider = row[provider_at].strip()
            wanted = provider in ccns
            cases = _count(row[cases_at])
            if cases is None:
                if wanted:
                    area.suppressed[provider] = area.suppressed.get(provider, 0) + 1
                continue
            zip_code = _zip5(row[zip_at])
            if not zip_code:
                continue  # a row with no ZIP says where nobody lives
            area.zip_totals[zip_code] = area.zip_totals.get(zip_code, 0) + cases
            if wanted:
                by_zip = area.cases.setdefault(provider, {})
                by_zip[zip_code] = by_zip.get(zip_code, 0) + cases
    return area


def acs_vintage(year: int) -> str:
    return f"ACS 5-year {year - 4}-{year}, table B17024"


def _acs_cell(value: Any) -> int:
    """An ACS estimate as an int; the "not available" codes (negative) and nulls are zero."""
    if value is None:
        return 0
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def parse_acs_b17024(payload: list[list[Any]]) -> dict[str, int]:
    """zcta -> people 65 and over under 200 % of poverty, from the API's array of arrays."""
    if not payload:
        return {}
    header = [str(name) for name in payload[0]]
    try:
        zcta_at = header.index(ACS_ZCTA_COLUMN)
        variable_at = [header.index(name) for name in ACS_VARIABLES]
    except ValueError as error:
        raise ValueError(f"ACS answer lacks the B17024 columns: {header}") from error
    poor: dict[str, int] = {}
    for row in payload[1:]:
        if len(row) <= zcta_at:
            continue
        zcta = _zip5(str(row[zcta_at]))
        poor[zcta] = sum(_acs_cell(row[at]) for at in variable_at if at < len(row))
    return poor


def read_acs_summary_dat(path: Path) -> list[list[str]]:
    """The ZCTA rows of the table-based summary file, reshaped as the API answers: a header of
    NAME, the sixteen variables and the ZCTA column, then one row per ZCTA. Streamed: the file
    holds 300,000 geographies of which 34,000 are ZCTAs."""
    # The summary file spells B17024_107E as B17024_E107.
    summary_names = [f"{name[:7]}E{name[7:10]}" for name in ACS_VARIABLES]
    rows: list[list[str]] = [["NAME", *ACS_VARIABLES, ACS_ZCTA_COLUMN]]
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter="|")
        header = [name.strip() for name in next(reader, [])]
        try:
            geoid_at = header.index("GEO_ID")
            variable_at = [header.index(name) for name in summary_names]
        except ValueError as error:
            raise ValueError(f"{path.name} lacks the B17024 columns: {header[:5]}") from error
        for row in reader:
            if len(row) <= geoid_at or not row[geoid_at].startswith(ACS_ZCTA_GEOID):
                continue
            zcta = _zip5(row[geoid_at][len(ACS_ZCTA_GEOID) :])
            cells = [row[at] if at < len(row) else "" for at in variable_at]
            rows.append([f"ZCTA5 {zcta}", *cells, zcta])
    return rows


def _json_array(path: Path) -> list[Any] | None:
    """The file's JSON array, or None when it holds anything else (a refusal page, a half
    answer)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, list) else None


def fetch_acs_b17024(
    http: httpx.Client, cache_dir: Path, year: int = 2024, refresh: bool = False
) -> dict[str, int]:
    """zcta -> people 65 and over under 200 % of poverty, for every ZCTA. One API request when
    the API allows it, else the summary file; cached as `cache_dir/acs/b17024_<year>.json`."""
    acs_dir = cache_dir / "acs"
    cached = acs_dir / f"b17024_{year}.json"
    if refresh or _json_array(cached) is None:
        url = ACS_URL.format(year=year, variables=",".join(ACS_VARIABLES))
        try:
            download.fetch_file(http, url, acs_dir, name=cached.name, refresh=True)
        except httpx.HTTPError as error:
            log.warning("the Census API did not answer (%s); reading the summary file", error)
        if _json_array(cached) is None:
            # A "Missing Key" page or no answer at all: never leave it under the cache name.
            cached.unlink(missing_ok=True)
            log.warning("the Census API wants a key; reading the summary file instead")
            summary = download.fetch_file(
                http, ACS_SUMMARY_URL.format(year=year), acs_dir, refresh=refresh
            )
            partial = cached.with_name(cached.name + ".part")
            partial.write_text(json.dumps(read_acs_summary_dat(summary)), encoding="utf-8")
            partial.replace(cached)
    payload = _json_array(cached)
    if payload is None:
        raise ValueError(f"{cached} is not a JSON array")
    return parse_acs_b17024(payload)


def compute_exposure(
    inpatient: dict[str, InpatientFacts],
    area: ServiceArea,
    poor_seniors: dict[str, int],
    ccns: set[str],
) -> dict[str, ExposureFacts]:
    """One `ExposureFacts` per wanted CCN, None wherever an input is missing."""
    facts: dict[str, ExposureFacts] = {}
    for ccn in sorted(ccns):
        counts = inpatient.get(ccn)
        discharges = counts.discharges if counts else None
        benes = counts.benes if counts else None
        dual_benes = counts.dual_benes if counts else None
        dual_discharges = None
        if discharges is not None and benes and dual_benes is not None:
            dual_discharges = discharges * dual_benes / benes
        by_zip = area.cases.get(ccn, {})
        served = None
        if by_zip:
            served = sum(
                poor_seniors.get(zip_code, 0) * cases / area.zip_totals[zip_code]
                for zip_code, cases in by_zip.items()
                if area.zip_totals.get(zip_code)
            )
        facts[ccn] = ExposureFacts(
            ccn=ccn,
            discharges=discharges,
            benes=benes,
            dual_benes=dual_benes,
            dual_discharges=dual_discharges,
            zips_counted=len(by_zip),
            zips_suppressed=area.suppressed.get(ccn, 0),
            poor_seniors_served=served,
        )
    return facts


def _dataset(catalog: list[dict[str, Any]], title: str) -> download.DatasetRef:
    """The newest version of exactly this dataset family. `latest_dataset` matches by title
    prefix, and the catalogue lists "... - by Provider and Service" beside "... - by Provider"
    with the same period and day, so the family is narrowed to "<title> : <date>" first."""
    family = [
        dataset
        for dataset in catalog
        if (full := str(dataset.get("title") or "").strip()) == title
        or full.lower().startswith(f"{title.lower()} :")
    ]
    ref = download.latest_dataset(family, title)
    if ref is None or not ref.csv_url:
        raise ValueError(f"the CMS catalogue lists no CSV for {title!r}")
    return ref


def load_exposure(
    session: Session,
    http: httpx.Client,
    cache_dir: Path,
    catalog: list[dict[str, Any]],
    *,
    today: date,
    refresh: bool = False,
    acs_year: int = 2024,
    ccns: set[str] | None = None,
) -> ExposureReport:
    """Fetch the three files (cached under `cache_dir`), compute every registry hospital's
    exposure inputs (or `ccns`; the demo hospital never) and upsert them into `ledger_exposure`."""
    inpatient_ref = _dataset(catalog, INPATIENT_TITLE)
    service_ref = _dataset(catalog, SERVICE_AREA_TITLE)
    inpatient_path = download.fetch_file(
        http, inpatient_ref.csv_url or "", cache_dir / "inpatient", refresh=refresh
    )
    area_path = download.fetch_file(
        http, service_ref.csv_url or "", cache_dir / "service_area", refresh=refresh
    )
    poor_seniors = fetch_acs_b17024(http, cache_dir, year=acs_year, refresh=refresh)

    registry = ccns if ccns is not None else {row.ccn for row in list_hospitals(session)}
    wanted = {ccn for ccn in registry if not is_demo(ccn)}
    inpatient = parse_inpatient_csv(inpatient_path)
    area = parse_service_area_csv(area_path, wanted)
    facts = compute_exposure(inpatient, area, poor_seniors, wanted)

    report = ExposureReport(hospitals=len(wanted), acs_zctas=len(poor_seniors))
    served_zips: set[str] = set()
    for ccn, fact in facts.items():
        has_inpatient = ccn in inpatient
        has_area = fact.zips_counted > 0
        report.with_inpatient += has_inpatient
        report.with_service_area += has_area
        report.with_both += has_inpatient and has_area
        served_zips.update(area.cases.get(ccn, {}))
        upsert_exposure(
            session,
            ccn,
            discharges=fact.discharges,
            benes=fact.benes,
            dual_benes=fact.dual_benes,
            dual_discharges=fact.dual_discharges,
            zips_counted=fact.zips_counted,
            zips_suppressed=fact.zips_suppressed,
            poor_seniors_served=fact.poor_seniors_served,
            inpatient_vintage=download.vintage_of(inpatient_ref),
            service_area_vintage=download.vintage_of(service_ref),
            acs_vintage=acs_vintage(acs_year),
            loaded_on=today,
        )
    report.zips_matched_to_acs = len(served_zips & poor_seniors.keys())
    log.info(
        "exposure: %d hospitals, %d with inpatient counts, %d with a service area, %d with both; "
        "%d of their ZIPs in the ACS (%d ZCTAs)",
        report.hospitals,
        report.with_inpatient,
        report.with_service_area,
        report.with_both,
        report.zips_matched_to_acs,
        report.acs_zctas,
    )
    return report
