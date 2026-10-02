from datetime import date

from waive.atlas import repo
from waive.atlas.overlays import STATE_OVERLAYS, apply_overlay, find_quote, run_overlay
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import SAMPLE_POLICY_TEXT, st_example_sheet
from waive.atlas.schema import SourceDoc, SourceKind
from waive.atlas.tavily_gateway import ExtractedPage, SearchHit
from waive.atlas.verify import verify_sheet
from waive.db import init_db, make_engine, session_scope

TODAY = date(2026, 10, 2)
TEXT = (
    "Welcome to the page. The Health Safety Net pays acute care hospitals and community health "
    "centers for certain services to low-income uninsured and underinsured Massachusetts residents. "
    "Other text follows."
)
SPEC = STATE_OVERLAYS["MA"]


def test_find_quote_returns_the_sentence_with_the_phrase():
    quote = find_quote(TEXT, "Health Safety Net")
    assert quote.startswith("The Health Safety Net pays") and quote.endswith("residents.")
    assert find_quote("nothing here.", "Health Safety Net") is None


def test_apply_overlay_adds_cited_state_program():
    source = SourceDoc(
        id="state-ma-hsn",
        kind=SourceKind.STATE_REPOSITORY,
        url="https://www.mass.gov/x",
        title="HSN",
        fetched_on=TODAY,
        sha256="0" * 64,
    )
    sheet = apply_overlay(st_example_sheet(), source, TEXT, SPEC, TODAY)
    program = sheet.programs.state_programs.value[0]
    assert program.name == SPEC.program_name
    assert sheet.programs.state_programs.source_id == "state-ma-hsn"
    assert any(s.id == "state-ma-hsn" for s in sheet.sources)
    assert verify_sheet(sheet, {"state-ma-hsn": TEXT, sheet.sources[0].id: SAMPLE_POLICY_TEXT}).ok


class FakeGateway:
    def search(self, query, **kwargs):
        assert kwargs["include_domains"] == ["mass.gov"]
        return [SearchHit("https://www.mass.gov/hsn", "Health Safety Net", "", 0.9)]

    def extract(self, urls, **kwargs):
        return [ExtractedPage(urls[0], TEXT)]


def test_run_overlay_publishes_new_versions():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    sheet = st_example_sheet()
    with session_scope(engine) as session:
        repo.upsert_hospital(
            session,
            {
                "ccn": sheet.hospital.ccn,
                "name": sheet.hospital.name,
                "city": "BOSTON",
                "state": "MA",
                "zip": "02118",
                "hospital_type": "Acute Care Hospitals",
                "ownership": "Voluntary non-profit - Private",
            },
        )
        publish_sheet(session, sheet)
        assert run_overlay(session, FakeGateway(), "MA", TODAY) == 1
        assert run_overlay(session, FakeGateway(), "MA", TODAY) == 0
        latest, _ = repo.latest_sheet(session, sheet.hospital.ccn)
        assert latest.version == 2 and latest.programs.state_programs is not None
