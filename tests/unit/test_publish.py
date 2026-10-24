import json
from decimal import Decimal

from waive.atlas import repo
from waive.atlas.publish import (
    CRITICAL_PATHS,
    critical_conflicts,
    decide_status,
    diff_sheets,
    drop_fields,
    export_state,
    publish_sheet,
    resolve_conflicts,
    sheet_inconsistencies,
)
from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import DiscountTier, SheetStatus
from waive.db import init_db, make_engine, session_scope

SAMPLE = st_example_sheet()
HOSPITAL = {
    "ccn": "229999",
    "name": "ST. EXAMPLE MEDICAL CENTER",
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
}
REAL_HOSPITAL = {**HOSPITAL, "ccn": "220031", "name": "REAL GENERAL HOSPITAL", "city": "WORCESTER"}
REAL = SAMPLE.model_copy(
    update={
        "hospital": SAMPLE.hospital.model_copy(
            update={"ccn": "220031", "name": "Real General Hospital", "city": "Worcester"}
        )
    }
)


FREE_CARE = "eligibility.free_care_max_fpl"
PRESUMPTIVE = "programs.presumptive"


def with_free_limit(sheet, value, quote=None):
    update = {"value": Decimal(value)}
    if quote is not None:
        update["quote"] = quote
    cited = sheet.eligibility.free_care_max_fpl.model_copy(update=update)
    return sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"free_care_max_fpl": cited})}
    )


def with_presumptive(sheet, programs):
    cited = sheet.programs.presumptive.model_copy(update={"value": programs})
    return sheet.model_copy(
        update={"programs": sheet.programs.model_copy(update={"presumptive": cited})}
    )


def test_resolve_conflicts_keeps_primary_when_the_tiebreak_agrees_with_it():
    secondary = with_free_limit(SAMPLE, 300)
    merged, remaining, detail = resolve_conflicts(SAMPLE, secondary, SAMPLE, [FREE_CARE])
    assert merged == SAMPLE
    assert remaining == []
    assert detail == {
        FREE_CARE: {"primary": "250", "secondary": "300", "tiebreak": "250", "verdict": "primary"}
    }


def test_resolve_conflicts_takes_the_secondary_cited_when_the_tiebreak_sides_with_it():
    secondary = with_free_limit(SAMPLE, 400, quote="above 250% and at or below 400%")
    tiebreak = with_free_limit(SAMPLE, 400, quote="a different quote from the third model")
    merged, remaining, detail = resolve_conflicts(SAMPLE, secondary, tiebreak, [FREE_CARE])
    assert remaining == []
    assert detail[FREE_CARE]["verdict"] == "secondary"
    assert merged.eligibility.free_care_max_fpl == secondary.eligibility.free_care_max_fpl
    assert merged.eligibility.free_care_max_fpl.quote == "above 250% and at or below 400%"
    # Everything else is still the primary's.
    assert merged.eligibility.discount_tiers == SAMPLE.eligibility.discount_tiers
    assert merged.programs == SAMPLE.programs and merged.apply == SAMPLE.apply


def test_resolve_conflicts_keeps_the_conflict_when_the_tiebreak_settles_nothing():
    secondary = with_free_limit(SAMPLE, 300)
    disagreeing = with_free_limit(SAMPLE, 999)
    missing = drop_fields(SAMPLE, [FREE_CARE])
    for tiebreak, third in ((disagreeing, "999"), (missing, None)):
        merged, remaining, detail = resolve_conflicts(SAMPLE, secondary, tiebreak, [FREE_CARE])
        assert merged == SAMPLE
        assert remaining == [FREE_CARE]
        assert detail[FREE_CARE] == {
            "primary": "250",
            "secondary": "300",
            "tiebreak": third,
            "verdict": "unsettled",
        }


def test_resolve_conflicts_settles_each_path_on_its_own():
    secondary = with_presumptive(with_free_limit(SAMPLE, 300), ["MassHealth", "SNAP", "WIC"])
    tiebreak = with_presumptive(
        SAMPLE, ["Medicare"]
    )  # free-care limit as primary, programs as neither
    merged, remaining, detail = resolve_conflicts(
        SAMPLE, secondary, tiebreak, [FREE_CARE, PRESUMPTIVE]
    )
    assert merged == SAMPLE
    assert remaining == [PRESUMPTIVE]
    assert detail[FREE_CARE]["verdict"] == "primary"
    assert detail[PRESUMPTIVE]["verdict"] == "unsettled"
    assert detail[PRESUMPTIVE]["tiebreak"] == ["Medicare"]


def test_drop_fields_removes_only_named_paths():
    sheet = drop_fields(SAMPLE, ["contacts.phone", "eligibility.discount_tiers"])
    paths = [path for path, _ in sheet.field_paths()]
    assert "contacts.phone" not in paths and "eligibility.discount_tiers" not in paths
    assert "eligibility.free_care_max_fpl" in paths


def test_critical_conflicts_only_on_disagreement():
    assert critical_conflicts(SAMPLE, SAMPLE) == []
    assert critical_conflicts(SAMPLE, with_free_limit(SAMPLE, 300)) == [
        "eligibility.free_care_max_fpl"
    ]
    missing = drop_fields(SAMPLE, ["eligibility.free_care_max_fpl"])
    assert critical_conflicts(SAMPLE, missing) == []


def test_decide_status():
    assert decide_status(SAMPLE, []) is SheetStatus.PUBLISHED
    assert decide_status(SAMPLE, ["eligibility.free_care_max_fpl"]) is SheetStatus.HELD
    no_limits = drop_fields(SAMPLE, ["eligibility.free_care_max_fpl", "eligibility.discount_tiers"])
    assert decide_status(no_limits, []) is SheetStatus.HELD


CEILING = "eligibility.assistance_ceiling_fpl"
CEILING_SENTENCE = (
    "Patients with household income up to 400% of the Federal Poverty Guidelines may qualify for "
    "financial assistance."
)


def with_ceiling(sheet, value, quote=CEILING_SENTENCE):
    """`sheet` with only an assistance ceiling for income rules (decision 7.9a-2)."""
    cited = sheet.eligibility.free_care_max_fpl.model_copy(
        update={"value": Decimal(value), "quote": quote}
    )
    eligibility = sheet.eligibility.model_copy(
        update={"free_care_max_fpl": None, "discount_tiers": None, "assistance_ceiling_fpl": cited}
    )
    return sheet.model_copy(update={"eligibility": eligibility})


def test_a_ceiling_with_no_band_or_table_may_publish_unless_something_else_holds_it():
    # Task 7.14: the verified ceiling is enough to publish on; every other hold still holds.
    ceiling_only = with_ceiling(SAMPLE, 400)
    assert decide_status(ceiling_only, []) is SheetStatus.PUBLISHED
    assert decide_status(ceiling_only, [CEILING]) is SheetStatus.HELD
    hold = "the hospital's website was matched with low confidence and is not confirmed yet"
    assert decide_status(ceiling_only, [], [hold]) is SheetStatus.HELD
    assert decide_status(drop_fields(ceiling_only, [CEILING]), []) is SheetStatus.HELD
    assert sheet_inconsistencies(ceiling_only) == []


def test_two_models_disagreeing_on_the_ceiling_is_a_critical_conflict():
    assert CEILING in CRITICAL_PATHS
    first = with_ceiling(SAMPLE, 400)
    assert critical_conflicts(first, first) == []
    assert critical_conflicts(first, with_ceiling(SAMPLE, 300)) == [CEILING]
    # Review of 7.14: a ceiling-only sheet against a (verified) band or table is disputed too:
    # the documents state a band the ceiling-only answer leaves out.
    assert critical_conflicts(first, SAMPLE) == [CEILING]
    band_only = drop_fields(SAMPLE, ["eligibility.discount_tiers"])
    table_only = drop_fields(SAMPLE, ["eligibility.free_care_max_fpl"])
    assert critical_conflicts(first, band_only) == [CEILING]
    assert critical_conflicts(first, table_only) == [CEILING]
    # A model that found nothing disputes nothing; and a band or a table the primary publishes is
    # not held for a cross-check that read only a ceiling (the sheet reads as it did before 7.14).
    nothing = drop_fields(SAMPLE, ["eligibility.free_care_max_fpl", "eligibility.discount_tiers"])
    assert critical_conflicts(first, nothing) == []
    assert critical_conflicts(SAMPLE, first) == []


def with_tiers(sheet, bands):
    tiers = [
        DiscountTier(
            min_fpl_exclusive=Decimal(low), max_fpl_inclusive=Decimal(high), discount_percent=pct
        )
        for low, high, pct in bands
    ]
    cited = sheet.eligibility.discount_tiers.model_copy(update={"value": tiers})
    return sheet.model_copy(
        update={"eligibility": sheet.eligibility.model_copy(update={"discount_tiers": cited})}
    )


def test_a_free_limit_that_swallows_the_discount_table_holds_the_sheet():
    # Brigham (220110): "discounts are limited to incomes up to 300% FPG" was read as free care
    # while the same document's table gives 100% only to 150%, then 85% and 70% up to 300%.
    swallowed = with_tiers(with_free_limit(SAMPLE, 300), [(150, 250, 85), (250, 300, 70)])
    problems = sheet_inconsistencies(swallowed)
    assert len(problems) == 1 and problems[0].startswith("eligibility.free_care_max_fpl: 300%")
    assert "150%" in problems[0]
    assert decide_status(swallowed, []) is SheetStatus.HELD
    assert (
        sheet_inconsistencies(SAMPLE) == []
        and sheet_inconsistencies(with_free_limit(SAMPLE, 250)) == []
    )
    # A limit at the first band's lower bound is the normal shape (free to 250, then 60% off).
    assert decide_status(with_tiers(with_free_limit(SAMPLE, 250), [(250, 400, 60)]), []) is (
        SheetStatus.PUBLISHED
    )


def test_a_very_high_free_limit_with_no_table_is_held_for_review():
    # UMass Memorial (220163): "less than 600% of the federal poverty guidelines" is the program's
    # ceiling (with an AGB cap), not a free-care band.
    tall = drop_fields(with_free_limit(SAMPLE, 600), ["eligibility.discount_tiers"])
    assert decide_status(tall, []) is SheetStatus.HELD
    assert "600%" in sheet_inconsistencies(tall)[0]
    usual = drop_fields(with_free_limit(SAMPLE, 400), ["eligibility.discount_tiers"])
    assert decide_status(usual, []) is SheetStatus.PUBLISHED


def test_diff_sheets():
    assert diff_sheets(SAMPLE, SAMPLE) == {}
    changed = with_free_limit(SAMPLE, 300)
    assert diff_sheets(SAMPLE, changed) == {
        "eligibility.free_care_max_fpl": {"old": "250", "new": "300"}
    }
    assert diff_sheets(None, SAMPLE)["eligibility.free_care_max_fpl"] == {"old": None, "new": "250"}


def test_publish_bumps_version_only_on_change(tmp_path):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, REAL_HOSPITAL)
        first = publish_sheet(session, REAL)
        again = publish_sheet(session, REAL)
        second = publish_sheet(session, with_free_limit(REAL, 300))
        assert (first.version, again, second.version) == (1, None, 2)
        assert second.diff == {"eligibility.free_care_max_fpl": {"old": "250", "new": "300"}}
        out = tmp_path / "ma.json"
        assert export_state(session, "MA", out) == 1
    data = json.loads(out.read_text())
    assert data["license"] == "CC BY 4.0"
    assert data["sheets"][0]["version"] == 2


def test_export_leaves_the_fictional_demo_hospital_out(tmp_path):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        repo.upsert_hospital(session, REAL_HOSPITAL)
        assert publish_sheet(session, SAMPLE).version == 1
        assert publish_sheet(session, REAL).version == 1
        # The demo sheet is still in the database for the phone demo ...
        assert repo.latest_sheet(session, "229999") is not None
        out = tmp_path / "ma.json"
        # ... but the public export only carries real hospitals.
        assert export_state(session, "MA", out) == 1
    data = json.loads(out.read_text())
    assert [sheet["hospital"]["ccn"] for sheet in data["sheets"]] == ["220031"]
    assert "229999" not in out.read_text()


def test_export_carries_published_sheets_only(tmp_path):
    # Audit rules-correctness-9: data/atlas/*.json carried 253 held sheets. A hospital whose newest
    # version is held is left out, not exported from the published version that hold withdrew.
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    third = {**REAL_HOSPITAL, "ccn": "220032", "name": "THIRD HOSPITAL"}
    with session_scope(engine) as session:
        repo.upsert_hospital(session, REAL_HOSPITAL)
        repo.upsert_hospital(session, third)
        publish_sheet(session, REAL)
        other = REAL.model_copy(
            update={"hospital": REAL.hospital.model_copy(update={"ccn": "220032"})}
        )
        publish_sheet(session, other)
        publish_sheet(session, other.model_copy(update={"status": SheetStatus.HELD}))
        out = tmp_path / "ma.json"
        assert export_state(session, "MA", out) == 1
    data = json.loads(out.read_text())
    assert [(s["hospital"]["ccn"], s["status"]) for s in data["sheets"]] == [
        ("220031", "published")
    ]


def test_a_discount_that_rises_with_income_holds_the_sheet():
    # Adventist Health (050013, 7.9): tiers 50/75/75 published from a patient-responsibility
    # table. The publish-time check is the safety net behind verify's patient-share rule.
    upside_down = with_tiers(SAMPLE, [(250, 300, 50), (300, 400, 75)])
    problems = sheet_inconsistencies(upside_down)
    assert len(problems) == 1 and problems[0].startswith("eligibility.discount_tiers:")
    assert "75%" in problems[0] and "50%" in problems[0]
    assert decide_status(upside_down, []) is SheetStatus.HELD
    assert sheet_inconsistencies(with_tiers(SAMPLE, [(250, 300, 75), (300, 400, 50)])) == []


def test_documents_that_name_a_different_free_care_limit_are_a_conflict():
    from waive.atlas.publish import document_conflicts
    from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID
    from waive.atlas.schema import SourceKind

    # AHMC Anaheim (050226, review of 7.9): the web page said 250%, the newer PDF 200%.
    newer = SAMPLE.sources[0].model_copy(update={"id": "fap-newer", "url": None})
    sheet = SAMPLE.model_copy(update={"sources": [*SAMPLE.sources, newer]})
    newer_text = (
        "Patients whose family income is at or below 200 percent of the Federal Poverty Level "
        "will be eligible for a 100 percent write-off."
    )
    docs = {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT, "fap-newer": newer_text}
    assert document_conflicts(sheet, docs) == [{"id": "fap-newer", "limits": ["200"]}]
    # The same limit, a document naming none, or a missing text: no conflict.
    assert document_conflicts(sheet, {**docs, "fap-newer": SAMPLE_POLICY_TEXT}) == []
    assert document_conflicts(sheet, {**docs, "fap-newer": "Call us about financial help."}) == []
    assert document_conflicts(sheet, {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT}) == []
    # Only the hospital's web documents count: an admin-approved patient photo supersedes.
    photo = newer.model_copy(update={"kind": SourceKind.PATIENT_PHOTO})
    assert (
        document_conflicts(sheet.model_copy(update={"sources": [*SAMPLE.sources, photo]}), docs)
        == []
    )
    assert document_conflicts(drop_fields(sheet, ["eligibility.free_care_max_fpl"]), docs) == []


def test_a_ceiling_only_sheet_conflicts_with_a_document_that_states_a_free_care_limit():
    from waive.atlas.publish import document_conflicts
    from waive.atlas.samples import SAMPLE_SOURCE_ID
    from waive.atlas.schema import SourceKind

    # Task 7.14: a sheet that publishes only the ceiling says the documents name no free-care
    # band. One of the hospital's documents naming one (its own included) is a band the sheet
    # lacks, and which is right is an admin's call, as for two free-care limits.
    summary = f"Financial Assistance Summary. {CEILING_SENTENCE}"
    free_text = (
        "Patients whose family income is at or below 200 percent of the Federal Poverty Level "
        "will be eligible for a 100 percent write-off."
    )
    policy = SAMPLE.sources[0].model_copy(update={"id": "fap-policy", "url": None})
    sheet = with_ceiling(SAMPLE, 400).model_copy(update={"sources": [*SAMPLE.sources, policy]})
    docs = {SAMPLE_SOURCE_ID: summary, "fap-policy": free_text}
    assert document_conflicts(sheet, docs) == [{"id": "fap-policy", "limits": ["200"]}]
    assert document_conflicts(sheet, {**docs, SAMPLE_SOURCE_ID: summary + " " + free_text}) == [
        {"id": SAMPLE_SOURCE_ID, "limits": ["200"]},
        {"id": "fap-policy", "limits": ["200"]},
    ]
    # Documents that name no free-care limit leave the ceiling standing.
    assert document_conflicts(sheet, {**docs, "fap-policy": "Call us about financial help."}) == []
    # A patient's approved photo supersedes; a ceiling cited from one is not checked either.
    photo = policy.model_copy(update={"kind": SourceKind.PATIENT_PHOTO})
    assert (
        document_conflicts(sheet.model_copy(update={"sources": [*SAMPLE.sources, photo]}), docs)
        == []
    )
    from_photo = sheet.model_copy(
        update={
            "sources": [
                SAMPLE.sources[0].model_copy(update={"kind": SourceKind.PATIENT_PHOTO}),
                policy,
            ]
        }
    )
    assert document_conflicts(from_photo, docs) == []


def test_a_ceiling_only_sheet_conflicts_with_help_its_documents_give_above_the_ceiling():
    from waive.atlas.publish import (
        CEILING_CONFLICT_REASON,
        DOCUMENT_CONFLICT_REASON,
        conflict_reason,
        document_conflicts,
    )
    from waive.atlas.samples import SAMPLE_SOURCE_ID

    # Review of 7.14: "at or below 200% ... eligible for financial assistance" read as the
    # ceiling, while the next sentence gives a sliding scale up to 400%.
    first = "Patients at or below 200% of the Federal Poverty Level are eligible for financial assistance."
    sliding = (
        "Patients between 201% and 400% of the Federal Poverty Level receive a sliding scale "
        "discount."
    )
    sheet = with_ceiling(SAMPLE, 200, quote=first)
    assert document_conflicts(sheet, {SAMPLE_SOURCE_ID: f"{first} {sliding}"}) == [
        {"id": SAMPLE_SOURCE_ID, "limits": ["400"]}
    ]
    assert conflict_reason(sheet) == CEILING_CONFLICT_REASON
    assert conflict_reason(SAMPLE) == DOCUMENT_CONFLICT_REASON
    # The ceiling itself, help below it, another program's limit or a payment plan: no conflict.
    within = (
        f"{CEILING_SENTENCE} Patients up to 200% of the FPL receive a discount. Households up to "
        "600% of the FPL may apply to the state's Health Safety Net for financial assistance. "
        "Payment plans are available up to 700% of the FPL."
    )
    assert document_conflicts(with_ceiling(SAMPLE, 400), {SAMPLE_SOURCE_ID: within}) == []


def with_ceiling_beside(sheet, value, quote=CEILING_SENTENCE):
    """`sheet` with an assistance ceiling added, its band and table kept."""
    cited = sheet.eligibility.free_care_max_fpl.model_copy(
        update={"value": Decimal(value), "quote": quote}
    )
    eligibility = sheet.eligibility.model_copy(update={"assistance_ceiling_fpl": cited})
    return sheet.model_copy(update={"eligibility": eligibility})


RUSSELL_TIERS = [(70, 100, 95), (100, 125, 80), (125, 150, 60), (150, 175, 40), (175, 200, 25)]


def test_a_ceiling_below_the_sheets_own_income_rules_holds_the_sheet():
    # Russell Regional (171350): a ceiling of 70 read from the free-care line, under a table that
    # runs to 200%.
    russell = drop_fields(with_ceiling_beside(with_tiers(SAMPLE, RUSSELL_TIERS), 70), [FREE_CARE])
    assert sheet_inconsistencies(russell) == [
        "eligibility.assistance_ceiling_fpl: 70% is below the sheet's own income rules"
    ]
    assert decide_status(russell, []) is SheetStatus.HELD
    under_free = drop_fields(with_ceiling_beside(SAMPLE, 200), ["eligibility.discount_tiers"])
    assert sheet_inconsistencies(under_free) == [
        "eligibility.assistance_ceiling_fpl: 200% is below the sheet's own income rules"
    ]
    # A ceiling at or above every band and the free limit is the normal shape.
    usual = with_ceiling_beside(with_tiers(with_free_limit(SAMPLE, 200), [(200, 400, 60)]), 400)
    assert sheet_inconsistencies(usual) == []
    assert decide_status(usual, []) is SheetStatus.PUBLISHED
    assert sheet_inconsistencies(with_ceiling_beside(SAMPLE, 600)) == []


def test_a_gap_between_the_sheets_income_bands_holds_the_sheet():
    # University Health Lakewood (260102): free care to 100%, the first tier from 300%; the
    # 101-300% band is missing.
    lakewood = with_tiers(with_free_limit(SAMPLE, 100), [(300, 400, 25)])
    assert sheet_inconsistencies(lakewood) == [
        "eligibility.discount_tiers: no rule covers incomes from 100% to 300%"
    ]
    assert decide_status(lakewood, []) is SheetStatus.HELD
    between = with_tiers(with_free_limit(SAMPLE, 100), [(100, 200, 75), (250, 400, 50)])
    assert sheet_inconsistencies(between) == [
        "eligibility.discount_tiers: no rule covers incomes from 200% to 250%"
    ]
    # Bands written "171%" after "170%" leave no gap, nor does a band from the next whole percent.
    labels = with_tiers(with_free_limit(SAMPLE, 100), [(100, 170, 75), (171, 250, 55)])
    assert sheet_inconsistencies(labels) == []
    assert sheet_inconsistencies(with_tiers(with_free_limit(SAMPLE, 100), [(101, 200, 75)])) == []
    # Two points above it is a gap.
    assert sheet_inconsistencies(with_tiers(with_free_limit(SAMPLE, 100), [(102, 200, 75)])) == [
        "eligibility.discount_tiers: no rule covers incomes from 100% to 102%"
    ]
    # No free limit and a first band above 0 is no gap the sheet itself shows.
    table_only = drop_fields(with_tiers(SAMPLE, [(200, 300, 75), (300, 400, 50)]), [FREE_CARE])
    assert sheet_inconsistencies(table_only) == []
    assert decide_status(table_only, []) is SheetStatus.PUBLISHED


def test_an_owner_ruled_ceiling_is_not_held_below_the_sheets_own_rules():
    # Owner rulings win (review decision 2026-10-23): Baptist Health's "discount 300" makes 300
    # the ceiling next to its 301-400% band. Only the ruled value is spared.
    baptist = drop_fields(
        with_ceiling_beside(with_tiers(SAMPLE, [(300, 400, 50)]), 300), [FREE_CARE]
    )
    below = ["eligibility.assistance_ceiling_fpl: 300% is below the sheet's own income rules"]
    assert sheet_inconsistencies(baptist) == below
    assert decide_status(baptist, []) is SheetStatus.HELD
    assert sheet_inconsistencies(baptist, ruled_ceiling=Decimal(300)) == []
    assert decide_status(baptist, [], ruled_ceiling=Decimal(300)) is SheetStatus.PUBLISHED
    assert sheet_inconsistencies(baptist, ruled_ceiling=Decimal(200)) == below
    # The other checks still read a ruled sheet: a gap between its bands holds it.
    gapped = with_tiers(baptist, [(300, 350, 50), (380, 400, 25)])
    assert sheet_inconsistencies(gapped, ruled_ceiling=Decimal(300)) == [
        "eligibility.discount_tiers: no rule covers incomes from 350% to 380%"
    ]
