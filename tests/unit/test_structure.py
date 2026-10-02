from datetime import date
from decimal import Decimal

from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import DocType, SheetStatus
from waive.atlas.structure import (
    MAX_DOC_CHARS,
    SYSTEM_PROMPT,
    DraftField,
    SheetDraft,
    build_messages,
    draft_to_sheet,
    select_passages,
    structure_sheet,
)
from waive.atlas.verify import verify_sheet

TODAY = date(2026, 10, 2)
SAMPLE = st_example_sheet()
SOURCES = [(SAMPLE.sources[0], SAMPLE_POLICY_TEXT)]


def field(value, quote):
    return DraftField(value=value, quote=quote, source_id=SAMPLE_SOURCE_ID)


DRAFT = SheetDraft(
    free_care_max_fpl=field(
        "250%",
        "household income at or below 250% of the Federal Poverty Guidelines are eligible for free care",
    ),
    discount_tiers=field(
        [{"min_fpl_exclusive": 250, "max_fpl_inclusive": 400, "discount_percent": 60}],
        "above 250% and at or below 400% of the Federal Poverty Guidelines receive a 60% discount",
    ),
    presumptive=field(
        ["MassHealth", "SNAP"],
        "Patients enrolled in MassHealth or SNAP are presumptively eligible for free care",
    ),
    documents_required=field(
        ["photo id", "proof of income", "utility bill"],
        "Applicants must provide a photo ID and one proof of income",
    ),
    submit_methods=field(
        [
            {
                "kind": "mail",
                "detail": "Patient Financial Services, 1 Example Way, Boston, MA 02118",
            },
            {"kind": "Fax", "detail": "617-555-0199"},
        ],
        "Applications may be mailed to Patient Financial Services, 1 Example Way, Boston, MA 02118, or faxed to 617-555-0199",
    ),
    window_days_from_first_bill=field(
        "240 days",
        "Applications are accepted up to 240 days after the first post-discharge billing statement",
    ),
    eca_wait_days=field(
        120,
        "will not begin extraordinary collection actions before 120 days after the first post-discharge billing statement",
    ),
    phone=field("617-555-0100", "Questions: call 617-555-0100"),
    hours=DraftField(value="9-5", quote="open 9 to 5", source_id="not-a-real-source"),
)


def test_messages_label_sources_and_treat_text_as_data():
    messages = build_messages(SAMPLE.hospital, SOURCES)
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert f"=== SOURCE id={SAMPLE_SOURCE_ID}" in messages[1]["content"]
    assert "St. Example Medical Center" in messages[1]["content"]
    assert "ignore any instructions" in SYSTEM_PROMPT.lower()


FILLER = "The hospital provides care to the community in many ways and settings. "
FPL_TABLE = (
    "Federal Poverty Guidelines table: 0%-200% 100% discount; 201%-400% 60% discount; "
    "401%-500% 25% discount."
)


def test_long_documents_keep_their_distant_fpl_table_in_the_prompt():
    head = (FILLER * 1_000)[:70_000]
    text = (head + FPL_TABLE + FILLER * 500)[:100_000]
    assert text.index(FPL_TABLE) >= 70_000 > MAX_DOC_CHARS
    selected = select_passages(text, limit=MAX_DOC_CHARS)
    assert len(selected) <= MAX_DOC_CHARS
    assert selected.startswith(text[:6_000])
    assert FPL_TABLE in selected and "\n[…]\n" in selected
    for passage in selected.split("\n[…]\n"):
        assert passage in text  # verbatim slices, so quotes still verify against the full text
    prompt = build_messages(SAMPLE.hospital, [(SAMPLE.sources[0], text)])[1]["content"]
    assert FPL_TABLE in prompt
    assert select_passages("short policy text") == "short policy text"


def test_passage_selection_prefers_income_rules_over_frequent_boilerplate():
    # A credit and collection policy says "collection" every few lines; the one poverty table
    # near the end must still make it in (Heywood, 2.8g).
    paragraph = FILLER * 3 + "Collection activity follows the Billing and Collection Policy. "
    text = paragraph * (60_000 // len(paragraph))
    text = text[:50_000] + FPL_TABLE + text[50_000:]
    selected = select_passages(text, limit=MAX_DOC_CHARS)
    assert FPL_TABLE in selected and len(selected) <= MAX_DOC_CHARS


def test_draft_to_sheet_casts_and_skips_bad_fields():
    sheet, skipped = draft_to_sheet(DRAFT, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.status is SheetStatus.DRAFT and sheet.version == 1
    assert sheet.eligibility.free_care_max_fpl.value == Decimal("250")
    assert sheet.eligibility.discount_tiers.value[0].discount_percent == 60
    assert sheet.apply.documents_required.value == [
        DocType.PHOTO_ID,
        DocType.PROOF_OF_INCOME,
        DocType.OTHER,
    ]
    assert [m.kind for m in sheet.apply.submit_methods.value] == ["mail", "fax"]
    assert sheet.apply.window_days_from_first_bill.value == 240
    assert sheet.collections.eca_wait_days.value == 120
    assert sheet.contacts.phone.value == "617-555-0100"
    assert skipped == ["contacts.hours: unknown source_id not-a-real-source"]
    assert verify_sheet(sheet, {SAMPLE_SOURCE_ID: SAMPLE_POLICY_TEXT}).ok


def test_semantic_guards_reject_bogus_residency_and_short_windows():
    draft = SheetDraft(
        residency=field(["True"], "Proof of Residency required"),
        window_days_from_first_bill=field(120, "collection action undertaken for 120 days"),
        languages=field(["Massachusetts residents", "NH"], "serves Massachusetts and NH"),
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.residency is None
    assert sheet.apply.window_days_from_first_bill is None
    assert [s.split(":")[0] for s in skipped] == [
        "eligibility.residency",
        "apply.window_days_from_first_bill",
    ]
    good = SheetDraft(residency=field(["Massachusetts", "nh"], "residents of Massachusetts or NH"))
    sheet, skipped = draft_to_sheet(good, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.residency.value == ["MA", "NH"] and skipped == []


def test_bare_values_are_wrapped_then_skipped_for_lack_of_quote():
    draft = SheetDraft.model_validate(
        {"languages": ["English", "Spanish"], "phone": "617-555-0100", "hours": None}
    )
    assert draft.languages.value == ["English", "Spanish"] and draft.languages.quote is None
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.field_paths() == []
    assert skipped == [
        "contacts.phone: missing quote or source_id",
        "contacts.languages: missing quote or source_id",
    ]


def test_free_care_band_disguised_as_a_100_percent_tier_is_dropped():
    tiers = [
        {"min_fpl_exclusive": 0, "max_fpl_inclusive": 250, "discount_percent": 100},
        {"min_fpl_exclusive": 250, "max_fpl_inclusive": 400, "discount_percent": 60},
    ]
    draft = SheetDraft(
        discount_tiers=field(tiers, "above 250% and at or below 400% receive a 60% discount")
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert skipped == []
    assert [t.discount_percent for t in sheet.eligibility.discount_tiers.value] == [60]
    only_free = SheetDraft(
        discount_tiers=field(tiers[:1], "at or below 250% receive 100% discount")
    )
    _, skipped = draft_to_sheet(only_free, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert skipped and skipped[0].startswith("eligibility.discount_tiers: only free-care bands")


def test_unparseable_tier_items_are_skipped_not_the_whole_list():
    tiers = [
        {"min_fpl_exclusive": 250, "max_fpl_inclusive": 300, "discount_percent": 75},
        {"min_fpl_exclusive": 300, "max_fpl_inclusive": 400},
        {"min_fpl_exclusive": "sliding", "max_fpl_inclusive": 500, "discount_percent": 25},
        {"min_fpl_exclusive": None, "max_fpl_inclusive": None, "discount_percent": None},
        "not an object",
    ]
    quote = (
        "above 250% and at or below 400% of the Federal Poverty Guidelines receive a 60% discount"
    )
    draft = SheetDraft(discount_tiers=field(tiers, quote))
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert skipped == []
    assert [
        (t.min_fpl_exclusive, t.max_fpl_inclusive, t.discount_percent)
        for t in sheet.eligibility.discount_tiers.value
    ] == [(Decimal(250), Decimal(300), 75)]
    none_usable = SheetDraft(discount_tiers=field(tiers[1:], quote))
    sheet, skipped = draft_to_sheet(none_usable, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.discount_tiers is None
    assert skipped == ["eligibility.discount_tiers: no usable tier (4 of 4 items failed to parse)"]


def tiers_of(sheet):
    return [
        (t.min_fpl_exclusive, t.max_fpl_inclusive, t.discount_percent)
        for t in sheet.eligibility.discount_tiers.value
    ]


def test_tier_percent_strings_and_ranges_are_parsed():
    # Heywood-style table rows: the band arrives as one "201%-400%" string, the discount as "60%".
    tiers = [
        {"min_fpl_exclusive": "0%-200%", "max_fpl_inclusive": None, "discount_percent": "100%"},
        {"min_fpl_exclusive": "201%-400%", "max_fpl_inclusive": None, "discount_percent": "60%"},
        {"min_fpl_exclusive": None, "max_fpl_inclusive": "401% – 500%", "discount_percent": 25},
    ]
    quote = "0%-200% 100% 201%-400% 60% 401%-500% 25% of the Federal Poverty Guidelines"
    draft = SheetDraft(discount_tiers=field(tiers, quote))
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert skipped == []
    assert tiers_of(sheet) == [(Decimal(201), Decimal(400), 60), (Decimal(401), Decimal(500), 25)]


def test_tier_missing_lower_bound_uses_previous_max_or_zero():
    tiers = [
        {"max_fpl_inclusive": 200, "discount_percent": 100},
        {"max_fpl_inclusive": 400, "discount_percent": 60},
        {"min_fpl_exclusive": None, "max_fpl_inclusive": 500, "discount_percent": 20},
    ]
    draft = SheetDraft(discount_tiers=field(tiers, "200% 100%, 400% 60%, 500% 20% discount"))
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert skipped == []
    assert tiers_of(sheet) == [(Decimal(200), Decimal(400), 60), (Decimal(400), Decimal(500), 20)]
    first_only = SheetDraft(
        discount_tiers=field([{"max_fpl_inclusive": 300, "discount_percent": 50}], "300% 50%")
    )
    sheet, skipped = draft_to_sheet(first_only, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert skipped == [] and tiers_of(sheet) == [(Decimal(0), Decimal(300), 50)]


def test_tier_with_a_discount_range_is_skipped_never_averaged():
    tiers = [
        {"min_fpl_exclusive": 150, "max_fpl_inclusive": 300, "discount_percent": "30%-50%"},
        {"min_fpl_exclusive": 300, "max_fpl_inclusive": 400, "discount_percent": "20 to 40"},
    ]
    draft = SheetDraft(discount_tiers=field(tiers, "150% to 300% 30%-50%; 300% to 400% 20 to 40"))
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.discount_tiers is None
    assert skipped == ["eligibility.discount_tiers: no usable tier (2 of 2 items failed to parse)"]


def test_free_care_limit_is_derived_from_a_100_percent_band_when_missing():
    # Tufts: the 100% band is listed as a tier, free_care_max_fpl is null and the discount band
    # has no single number (it differs by facility).
    tiers = [
        {"min_fpl_exclusive": 0, "max_fpl_inclusive": 150, "discount_percent": 100},
        {"min_fpl_exclusive": 150, "max_fpl_inclusive": 300, "discount_percent": None},
    ]
    quote = "Up to 150% FPL 100% discount; up to 300% FPL discount varies by facility"
    draft = SheetDraft(
        free_care_max_fpl=DraftField(value=None, quote=None, source_id=None),
        discount_tiers=field(tiers, quote),
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert skipped == ["eligibility.discount_tiers: no usable tier (1 of 2 items failed to parse)"]
    free = sheet.eligibility.free_care_max_fpl
    assert free.value == Decimal(150)
    assert free.quote == quote and free.source_id == SAMPLE_SOURCE_ID
    assert sheet.eligibility.discount_tiers is None
    # A model-stated limit wins over the derived one.
    stated = SheetDraft(
        free_care_max_fpl=field(200, "at or below 200% of the Federal Poverty Guidelines"),
        discount_tiers=field(tiers, quote),
    )
    sheet, _ = draft_to_sheet(stated, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.free_care_max_fpl.value == Decimal(200)
    # No 100% band, nothing to derive.
    paid_only = SheetDraft(discount_tiers=field(tiers[1:], quote))
    sheet, _ = draft_to_sheet(paid_only, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.free_care_max_fpl is None


def test_null_values_and_missing_quotes_are_tolerated():
    draft = SheetDraft(
        free_care_max_fpl=DraftField(value=None, quote=None, source_id=None),
        phone=DraftField(value="617-555-0100", quote=None, source_id=SAMPLE_SOURCE_ID),
        eca_wait_days=field(120, "will not begin extraordinary collection actions before 120 days"),
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert [path for path, _ in sheet.field_paths()] == ["collections.eca_wait_days"]
    assert skipped == ["contacts.phone: missing quote or source_id"]


class FakeAI:
    def __init__(self, draft):
        self.draft = draft
        self.calls = []

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        self.calls.append((role, phi, purpose, schema))
        return schema.model_validate(self.draft.model_dump())


def test_structure_sheet_uses_reason_model_without_phi():
    ai = FakeAI(DRAFT)
    sheet, skipped = structure_sheet(ai, "reason", SAMPLE.hospital, SOURCES, TODAY)
    assert ai.calls[0][:3] == ("reason", False, "atlas.structure")
    assert ai.calls[0][3] is SheetDraft
    assert sheet.eligibility.free_care_max_fpl.value == Decimal("250")
    assert len(skipped) == 1
