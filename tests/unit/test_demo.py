import json
from datetime import date
from decimal import Decimal
from io import BytesIO

from PIL import Image
from typer.testing import CliRunner

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import SourceDoc, SourceKind
from waive.cases.extract import BillExtract
from waive.cases.match import is_confident, match_hospital
from waive.cases.synth import render_benefit_letter
from waive.cli import app
from waive.db import (
    CaseRow,
    ContributionRow,
    ReportedEvidenceRow,
    SheetRow,
    SourceDocRow,
    init_db,
    make_engine,
    session_scope,
)
from waive.demo import (
    DEMO_BILL,
    DEMO_LETTER_DATE,
    DEMO_MONTHLY_BENEFIT,
    forget_cases,
    reset_demo,
    seed_demo,
    write_demo_images,
)

from tests.unit.test_metrics import sheet_for
from tests.unit.test_pipeline import REAL_HOSPITAL


def memory_engine():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return engine


def test_seed_and_forget():
    engine = memory_engine()
    with session_scope(engine) as session:
        seed_demo(session)
        seed_demo(session)
        assert repo.latest_sheet(session, "229999")[0].version == 1
        session.add(CaseRow(id="x", state="MA", status="new", token_generation=1))
        # A bill's scout request holds the model's reading of a personal document (no CCN):
        # "all personal data" includes it. Real hospitals' review items stay.
        repo.add_review_item(
            session, None, "scout_request", {"hospital_name": "Rosa A. Clinic", "cases": ["h"]}
        )
        repo.add_review_item(session, "220031", "rescout_request", {"cases": ["h"], "count": 1})
    with session_scope(engine) as session:
        assert forget_cases(session) == 1
        assert [item.kind for item in repo.open_review_items(session)] == ["rescout_request"]


def test_demo_bill_is_rosa_and_matches_the_demo_hospital_confidently():
    # Spec §2 persona: one person, $1,900 a month, an $1,850 bill — free care under St. Example's
    # 250% rule. The same figures drive the web tests, so the video and the tests agree.
    assert DEMO_BILL.patient_name == "Rosa Alvarez"
    assert DEMO_BILL.amount_due == Decimal("1850.00")
    assert DEMO_BILL.statement_date == date(2026, 9, 3)
    assert DEMO_MONTHLY_BENEFIT * 12 == Decimal("22800.00")
    assert DEMO_LETTER_DATE < DEMO_BILL.statement_date
    extract = BillExtract(
        hospital_name=DEMO_BILL.hospital_name,
        hospital_phone=DEMO_BILL.hospital_phone,
        fap_phone=DEMO_BILL.fap_phone,
        fap_url=DEMO_BILL.fap_url,
    )
    candidates = match_hospital(extract, [st_example_sheet().hospital])
    assert candidates[0].ccn == "229999" and is_confident(candidates)


def test_render_benefit_letter_is_a_full_size_jpeg():
    jpeg = render_benefit_letter("Rosa Alvarez", Decimal("1900.00"), date(2026, 1, 10))
    assert jpeg[:3] == b"\xff\xd8\xff"
    assert Image.open(BytesIO(jpeg)).size == (1700, 2200)
    # Deterministic: the demo images never change between runs.
    assert jpeg == render_benefit_letter("Rosa Alvarez", Decimal("1900.00"), date(2026, 1, 10))


def test_write_demo_images_writes_bill_truth_and_letter(tmp_path):
    files = write_demo_images(tmp_path / "demo")
    assert [path.name for path in files] == ["bill.jpg", "bill.json", "letter.jpg"]
    assert (tmp_path / "demo" / "bill.jpg").read_bytes()[:3] == b"\xff\xd8\xff"
    truth = json.loads((tmp_path / "demo" / "bill.json").read_text())
    assert truth["hospital_name"] == "St. Example Medical Center"
    assert truth["amount_due"] == "1850.00" and truth["collection_notice"] is False
    before = {path.name: path.read_bytes() for path in files}
    assert write_demo_images(tmp_path / "demo") == files  # idempotent: the same paths...
    assert {path.name: path.read_bytes() for path in files} == before  # ...and the same bytes


def test_reset_demo_clears_cases_and_demo_rows_but_keeps_real_hospitals(tmp_path):
    engine = memory_engine()
    with session_scope(engine) as session:
        seed_demo(session)
        repo.upsert_hospital(session, REAL_HOSPITAL)
        publish_sheet(session, sheet_for(REAL_HOSPITAL))
        # A second demo sheet version, a case, and learning rows for both hospitals.
        session.add(
            SheetRow(
                ccn="229999",
                version=2,
                status="published",
                body=st_example_sheet().model_dump(mode="json"),
            )
        )
        session.add(
            CaseRow(id="case1", state="MA", ccn="229999", status="evaluated", token_generation=1)
        )
        repo.add_review_item(session, "229999", "rescout_request", {"count": 1})
        repo.add_review_item(session, REAL_HOSPITAL["ccn"], "rescout_request", {"count": 1})
        repo.add_review_item(session, None, "scout_request", {"hospital_name": "X", "cases": []})
        session.add(
            ContributionRow(
                ccn="229999",
                case_hash="a" * 16,
                photo_class="fap",
                sha256="b" * 64,
                created_on=date(2026, 10, 1),
            )
        )
        session.add(
            ReportedEvidenceRow(
                ccn="229999",
                field_path="apply.documents_required",
                value="photo_id",
                case_hash="c" * 16,
                created_on=date(2026, 10, 1),
            )
        )
        # Source documents: an approved demo-run patient photo (demo-only, goes), a state overlay
        # document held only by the demo hospital (shared by design, stays), and the real
        # hospital's own policy page (untouched).
        on = date(2026, 10, 1)
        photo = SourceDoc(
            id="photo-demo", kind=SourceKind.PATIENT_PHOTO, fetched_on=on, sha256="d" * 64
        )
        repo.save_source(session, photo, "screened photo text", "229999")
        state = SourceDoc(
            id="state-ma-hsn", kind=SourceKind.STATE_REPOSITORY, fetched_on=on, sha256="e" * 64
        )
        repo.save_source(session, state, "state program text", "229999")
        real = SourceDoc(
            id="fap-real",
            kind=SourceKind.HOSPITAL_WEB,
            url="https://real.example/fap",
            fetched_on=on,
            sha256="f" * 64,
        )
        repo.save_source(session, real, "policy text", REAL_HOSPITAL["ccn"])
    with session_scope(engine) as session:
        report = reset_demo(session, tmp_path / "demo")
        assert (report.cases_deleted, report.review_items_deleted) == (1, 2)  # + scout request
        assert (report.contributions_deleted, report.evidence_deleted) == (1, 1)
        assert (report.sources_unlinked, report.documents_deleted) == (2, 1)
        assert repo.sources_for(session, "229999") == []
        assert [doc.id for doc, _ in repo.sources_for(session, REAL_HOSPITAL["ccn"])] == [
            "fap-real"
        ]
        assert session.get(SourceDocRow, "photo-demo") is None
        assert session.get(SourceDocRow, "state-ma-hsn") is not None
        assert report.sheet_versions_deleted == 2
        assert report.sheet_version == 1
        assert [row.version for row in repo.sheet_versions(session, "229999")] == [1]
        assert repo.latest_sheet(session, REAL_HOSPITAL["ccn"])[0].version == 1
        assert len(repo.open_review_items(session, REAL_HOSPITAL["ccn"])) == 1
        assert repo.open_review_items(session, "229999") == []
        assert [path.name for path in report.files] == ["bill.jpg", "bill.json", "letter.jpg"]
    with session_scope(engine) as session:
        again = reset_demo(session, tmp_path / "demo", write_files=False)
        assert (again.cases_deleted, again.sheet_versions_deleted, again.files) == (0, 1, [])
        assert (again.sources_unlinked, again.documents_deleted) == (0, 0)


def test_demo_reset_command_prints_a_summary(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env here; the database URL comes from the environment
    monkeypatch.setenv("WAIVE_DATABASE_URL", f"sqlite:///{tmp_path / 'waive.db'}")
    result = CliRunner().invoke(app, ["demo", "reset", "--out", str(tmp_path / "demo")])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())  # the console wraps at 80 columns
    assert "Deleted 0 case(s)" in text and "version 1" in text
    assert "0 source link(s), 0 demo-only document(s)" in text
    assert (tmp_path / "demo" / "letter.jpg").exists()
