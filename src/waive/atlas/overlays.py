"""State programs layered onto hospital sheets, cited from official state pages (spec §7 programs)."""

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.schema import Cited, ProcedureSheet, SourceDoc, SourceKind, StateProgram
from waive.atlas.tavily_gateway import TavilyGateway


@dataclass(frozen=True)
class OverlaySpec:
    domain: str
    query: str
    program_name: str
    phrase: str
    how_to_apply: str


STATE_OVERLAYS = {
    "MA": OverlaySpec(
        domain="mass.gov",
        query="Health Safety Net eligibility hospital patients apply",
        program_name="Health Safety Net (Massachusetts)",
        phrase="Health Safety Net",
        how_to_apply=(
            "Apply through the MassHealth application; a hospital financial counselor can file it "
            "for you. The hospital's own financial assistance and the Health Safety Net are "
            "separate programs."
        ),
    )
}


def find_quote(text: str, phrase: str) -> str | None:
    for sentence in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text)):
        if phrase.lower() in sentence.lower() and 12 <= len(sentence) <= 400:
            return sentence.strip()
    return None


def fetch_overlay(
    gateway: TavilyGateway, spec: OverlaySpec, today: date
) -> tuple[SourceDoc, str] | None:
    hits = gateway.search(
        spec.query, purpose="atlas.overlay", include_domains=[spec.domain], max_results=3
    )
    for hit in hits:
        pages = gateway.extract([hit.url], purpose="atlas.overlay")
        if pages and find_quote(pages[0].text, spec.phrase):
            text = pages[0].text
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            source = SourceDoc(
                id=f"state-{spec.domain.split('.')[0]}-{sha[:12]}",
                kind=SourceKind.STATE_REPOSITORY,
                url=hit.url,
                title=hit.title or spec.program_name,
                fetched_on=today,
                sha256=sha,
            )
            return source, text
    return None


def apply_overlay(
    sheet: ProcedureSheet, source: SourceDoc, text: str, spec: OverlaySpec, today: date
) -> ProcedureSheet | None:
    quote = find_quote(text, spec.phrase)
    if quote is None:
        return None
    program = Cited[list[StateProgram]](
        value=[StateProgram(name=spec.program_name, how_to_apply=spec.how_to_apply)],
        quote=quote,
        source_id=source.id,
        checked_on=today,
    )
    sources = list(sheet.sources)
    if all(existing.id != source.id for existing in sources):
        sources.append(source)
    return sheet.model_copy(
        update={
            "programs": sheet.programs.model_copy(update={"state_programs": program}),
            "sources": sources,
        }
    )


def run_overlay(session: Session, gateway: TavilyGateway, state: str, today: date) -> int:
    spec = STATE_OVERLAYS.get(state.upper())
    if spec is None:
        return 0
    fetched = fetch_overlay(gateway, spec, today)
    if fetched is None:
        return 0
    source, text = fetched
    updated = 0
    for row in repo.list_hospitals(session, state=state):
        found = repo.latest_sheet(session, row.ccn)
        if found is None:
            continue
        sheet, _ = found
        existing = sheet.programs.state_programs
        if existing is not None and existing.source_id == source.id:
            continue
        new_sheet = apply_overlay(sheet, source, text, spec, today)
        if new_sheet is None:
            continue
        repo.save_source(session, source, text, row.ccn)
        if publish_sheet(session, new_sheet) is not None:
            updated += 1
    return updated
