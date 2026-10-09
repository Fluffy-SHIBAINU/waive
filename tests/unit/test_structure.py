from datetime import date
from decimal import Decimal

import pytest

from waive.ai.client import AIRequestRejected
from waive.atlas.publish import decide_status
from waive.atlas.samples import SAMPLE_POLICY_TEXT, SAMPLE_SOURCE_ID, st_example_sheet
from waive.atlas.schema import DocType, SheetStatus
from waive.atlas.structure import (
    MAX_DOC_CHARS,
    PASSAGE_HEAD_CHARS,
    PASSAGE_SEPARATOR,
    RETRY_DOC_CHARS,
    RETRY_HEAD_CHARS,
    RETRY_MAX_SOURCES,
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


def test_a_smaller_passage_budget_shrinks_the_head_and_the_windows():
    head = (FILLER * 1_000)[:70_000]
    text = (head + FPL_TABLE + FILLER * 500)[:100_000]
    smaller = select_passages(text, limit=RETRY_DOC_CHARS, head=RETRY_HEAD_CHARS)
    assert RETRY_DOC_CHARS == MAX_DOC_CHARS // 2 and RETRY_HEAD_CHARS == PASSAGE_HEAD_CHARS // 2
    assert len(smaller) <= RETRY_DOC_CHARS
    assert smaller.startswith(text[:RETRY_HEAD_CHARS] + PASSAGE_SEPARATOR)
    assert FPL_TABLE in smaller
    assert len(smaller) < len(select_passages(text, limit=MAX_DOC_CHARS))
    prompt = build_messages(
        SAMPLE.hospital, [(SAMPLE.sources[0], text)], limit=RETRY_DOC_CHARS, head=RETRY_HEAD_CHARS
    )[1]["content"]
    assert smaller in prompt and text[: RETRY_HEAD_CHARS + 1] not in prompt


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


def test_lists_and_objects_where_the_schema_wants_text_are_skipped_not_stringified():
    # Anna Jaques (CCN 220029): phone came back as [{"kind": "main", "number": ...}, ...] and
    # str() of that published a Python repr on the sheet page.
    draft = SheetDraft(
        phone=field(
            [{"kind": "main", "number": "617-555-0100"}, {"kind": "main", "number": "0101"}],
            "Questions: call 617-555-0100",
        ),
        hours=field(["9 to 5"], "Patient Financial Services is open 9 to 5"),
        presumptive=field(
            [{"name": "MassHealth"}, "SNAP"],
            "Patients enrolled in MassHealth or SNAP are presumptively eligible for free care",
        ),
        languages=field("English", "Interpreters are available in English"),
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.contacts.phone is None and sheet.contacts.hours is None
    assert sheet.programs.presumptive is None
    assert sheet.contacts.languages.value == ["English"]  # a bare string is still a one-item list
    assert skipped == [
        "programs.presumptive: list items must be text, not objects or lists",
        "contacts.phone: expected text, got a list or object",
        "contacts.hours: expected text, got a list or object",
    ]


def test_markdown_links_from_extracted_pages_are_unwrapped_in_text_fields():
    # UMass Memorial (CCN 220163): Tavily returns pages as markdown and the model copied
    # "[508-334-9300](tel:508-334-9300)" into a submit method, so the sheet showed the markup.
    draft = SheetDraft(
        phone=field("[508-334-9300](tel:508-334-9300)", "Telephone: [508-334-9300](tel:...)"),
        submit_methods=field(
            [{"kind": "email", "detail": "Email: [help@example.org](mailto:help@example.org)"}],
            "Email: [help@example.org](mailto:help@example.org)",
        ),
        form_url=field("[Apply online](https://example.org/apply.pdf)", "Apply online"),
        languages=field(["**English**", "[Spanish](https://example.org/es)"], "English, Spanish"),
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert skipped == []
    assert sheet.contacts.phone.value == "508-334-9300"
    assert sheet.apply.submit_methods.value[0].detail == "Email: help@example.org"
    assert sheet.apply.form_url.value == "https://example.org/apply.pdf"
    assert sheet.contacts.languages.value == ["English", "Spanish"]


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


def test_patient_share_guard_lives_in_verify_not_in_the_caster():
    # The caster cannot see the quote, so it still parses 15; verify_sheet rejects the tier when
    # the quote describes the patient's share. The prompt tells the model the difference too.
    quote = "201 - 250% FPL Patient Responsibility Co-Pay + 15% of total charges"
    draft = SheetDraft(
        discount_tiers=field(
            [{"min_fpl_exclusive": "201 - 250% FPL", "discount_percent": 15}], quote
        )
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert skipped == [] and tiers_of(sheet) == [(Decimal(201), Decimal(250), 15)]
    assert "never the share the patient pays" in SYSTEM_PROMPT


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
    # A model-stated limit wins over the derived one only while it fits under the 100% band.
    stated = SheetDraft(
        free_care_max_fpl=field(120, "at or below 120% of the Federal Poverty Guidelines"),
        discount_tiers=field(tiers, quote),
    )
    sheet, _ = draft_to_sheet(stated, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.free_care_max_fpl.value == Decimal(120)
    # Above the band it is a misread ceiling: the table wins and the swap is recorded.
    ceiling = SheetDraft(
        free_care_max_fpl=field(200, "at or below 200% of the Federal Poverty Guidelines"),
        discount_tiers=field(tiers, quote),
    )
    sheet, skipped = draft_to_sheet(ceiling, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.free_care_max_fpl.value == Decimal(150)
    assert "eligibility.free_care_max_fpl: stated 200 exceeds the 100% band (150)" in skipped[-1]
    # No 100% band, nothing to derive.
    paid_only = SheetDraft(discount_tiers=field(tiers[1:], quote))
    sheet, _ = draft_to_sheet(paid_only, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.free_care_max_fpl is None


def test_eligibility_ceiling_read_as_free_care_is_replaced_by_the_table():
    # Brigham (220110): the model stated 300 ("discounts ... limited to ... 300% FPG") while the
    # table it quoted gives 100% to 150%, 85% to 250% and 70% to 300%.
    tiers = [
        {"min_fpl_exclusive": 0, "max_fpl_inclusive": 150, "discount_percent": 100},
        {"min_fpl_exclusive": 150, "max_fpl_inclusive": 250, "discount_percent": 85},
        {"min_fpl_exclusive": 250, "max_fpl_inclusive": 300, "discount_percent": 70},
    ]
    quote = "0 to 150% 100% | 150.1 to 250% 85% | 251 to 300% 70%"
    draft = SheetDraft(
        free_care_max_fpl=field(
            300, "generally limited to patients with family incomes at or below 300%"
        ),
        discount_tiers=field(tiers, quote),
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    free = sheet.eligibility.free_care_max_fpl
    assert free.value == Decimal(150) and free.quote == quote
    assert tiers_of(sheet) == [(Decimal(150), Decimal(250), 85), (Decimal(250), Decimal(300), 70)]
    assert skipped == [
        "eligibility.free_care_max_fpl: stated 300 exceeds the 100% band (150); table used"
    ]
    assert "eligibility ceiling" in SYSTEM_PROMPT and "free_care_max_fpl" in SYSTEM_PROMPT


UMASS_CEILING = (
    "Financial assistance is available to patients and their family members with household "
    "income, less than 600% of the federal poverty guidelines."
)


def test_an_overall_assistance_ceiling_read_as_free_care_is_dropped():
    # UMass Memorial (220163): the summary bounds every kind of help at 600% FPG and names no
    # free-care band; the model put the same sentence in both fields.
    draft = SheetDraft(
        free_care_max_fpl=field(600, UMASS_CEILING),
        assistance_ceiling_fpl=field("600%", UMASS_CEILING),
        phone=field("508-334-9300", "Telephone: 508-334-9300"),
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.free_care_max_fpl is None
    assert sheet.eligibility.discount_tiers is None
    assert skipped == [
        "eligibility.free_care_max_fpl: stated 600 is at or above the overall assistance "
        "ceiling (600); not free care",
        "eligibility.free_care_max_fpl: not stated; the documents give only an overall "
        "assistance ceiling (600% FPL)",
    ]
    assert decide_status(sheet, []) is SheetStatus.HELD
    # The ceiling alone (the model followed the rules) leaves the same honest gap.
    ceiling_only = SheetDraft(assistance_ceiling_fpl=field(600, UMASS_CEILING))
    sheet, skipped = draft_to_sheet(ceiling_only, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert sheet.eligibility.free_care_max_fpl is None
    assert skipped == [
        "eligibility.free_care_max_fpl: not stated; the documents give only an overall "
        "assistance ceiling (600% FPL)"
    ]
    assert "assistance_ceiling_fpl" in SYSTEM_PROMPT


def test_a_free_care_band_under_the_ceiling_is_kept():
    tiers = [{"min_fpl_exclusive": 250, "max_fpl_inclusive": 400, "discount_percent": 60}]
    draft = SheetDraft(
        free_care_max_fpl=field(250, "at or below 250% of the Federal Poverty Guidelines"),
        assistance_ceiling_fpl=field(400, "assistance is limited to incomes up to 400%"),
        discount_tiers=field(tiers, "above 250% and at or below 400% receive a 60% discount"),
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    assert skipped == []
    assert sheet.eligibility.free_care_max_fpl.value == Decimal(250)
    assert tiers_of(sheet) == [(Decimal(250), Decimal(400), 60)]
    # A ceiling the model garbled, or cited from nowhere, changes nothing.
    for ceiling in (
        field("sliding scale", "assistance is limited"),
        DraftField(value=400, quote=None, source_id=SAMPLE_SOURCE_ID),
        DraftField(value=400, quote="limited to incomes up to 400%", source_id="not-a-source"),
    ):
        same = draft.model_copy(update={"assistance_ceiling_fpl": ceiling})
        sheet, skipped = draft_to_sheet(same, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
        assert skipped == [] and sheet.eligibility.free_care_max_fpl.value == Decimal(250)


def test_a_ceiling_misread_as_free_care_still_yields_to_the_100_percent_band():
    # Brigham's shape with the model also filling the ceiling: the stated 300 goes, the table's
    # 100% band supplies the real limit, and only one swap is recorded.
    tiers = [
        {"min_fpl_exclusive": 0, "max_fpl_inclusive": 150, "discount_percent": 100},
        {"min_fpl_exclusive": 150, "max_fpl_inclusive": 300, "discount_percent": 70},
    ]
    quote = "0 to 150% 100% | 150.1 to 300% 70%"
    limited = "generally limited to patients with family incomes at or below 300%"
    draft = SheetDraft(
        free_care_max_fpl=field(300, limited),
        assistance_ceiling_fpl=field(300, limited),
        discount_tiers=field(tiers, quote),
    )
    sheet, skipped = draft_to_sheet(draft, SAMPLE.hospital, [SAMPLE.sources[0]], TODAY)
    free = sheet.eligibility.free_care_max_fpl
    assert free.value == Decimal(150) and free.quote == quote
    assert tiers_of(sheet) == [(Decimal(150), Decimal(300), 70)]
    assert skipped == [
        "eligibility.free_care_max_fpl: stated 300 is at or above the overall assistance "
        "ceiling (300); not free care"
    ]
    # Without the stated limit the band is derived as before and the ceiling is not noted.
    _, skipped = draft_to_sheet(
        draft.model_copy(update={"free_care_max_fpl": None}),
        SAMPLE.hospital,
        [SAMPLE.sources[0]],
        TODAY,
    )
    assert skipped == []


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


REJECTED = AIRequestRejected(
    "nvidia/nemotron-3-super-120b-a12b: HTTP 400 (This model's maximum context length is "
    "131072 tokens. However, you requested 150000 tokens.)"
)


class RejectingAI(FakeAI):
    """Token Factory refuses the first `failures` requests, as it does for a prompt beyond the
    model's context window; the prompts it saw are kept for inspection."""

    def __init__(self, draft, failures=1):
        super().__init__(draft)
        self.failures = failures
        self.prompts: list[str] = []

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        self.prompts.append(messages[1]["content"])
        if len(self.prompts) <= self.failures:
            raise REJECTED
        return super().complete_json(role, messages, schema, phi=phi, purpose=purpose)


# A long policy whose income rules sit far from the head, so the passage budget matters.
LONG_TEXT = (SAMPLE_POLICY_TEXT + "\n" + (FILLER + "A discount may apply. ") * 2_000)[:50_000]


def test_a_rejected_request_is_retried_once_with_a_smaller_passage_budget():
    ai = RejectingAI(DRAFT)
    sheet, skipped = structure_sheet(
        ai, "reason", SAMPLE.hospital, [(SAMPLE.sources[0], LONG_TEXT)], TODAY
    )
    assert len(ai.prompts) == 2
    full = select_passages(LONG_TEXT, limit=MAX_DOC_CHARS)
    small = select_passages(LONG_TEXT, limit=RETRY_DOC_CHARS, head=RETRY_HEAD_CHARS)
    assert full in ai.prompts[0] and small in ai.prompts[1]
    assert len(small) < len(full) and len(ai.prompts[1]) < len(ai.prompts[0])
    assert sheet.eligibility.free_care_max_fpl.value == Decimal("250")
    # The operator learns the sheet came from a trimmed prompt, and why.
    assert skipped[0].startswith("structurer: retried with a smaller passage budget")
    assert "131072" in skipped[0]
    assert skipped[1:] == ["contacts.hours: unknown source_id not-a-real-source"]


def test_the_retry_shows_the_model_the_leading_sources_only():
    # Scouting lists the policy, application, summary and billing documents first (CLASS_ORDER),
    # so the leading four are the ones worth keeping when the whole set does not fit.
    sources = [(SAMPLE.sources[0], SAMPLE_POLICY_TEXT)] + [
        (SAMPLE.sources[0].model_copy(update={"id": f"extra-{i}"}), SAMPLE_POLICY_TEXT)
        for i in range(5)
    ]
    ai = RejectingAI(DRAFT)
    sheet, _ = structure_sheet(ai, "reason", SAMPLE.hospital, sources, TODAY)
    assert ai.prompts[0].count("=== SOURCE id=") == 6
    assert ai.prompts[1].count("=== SOURCE id=") == RETRY_MAX_SOURCES == 4
    assert "extra-4" not in ai.prompts[1] and "extra-3" not in ai.prompts[1]
    assert f"id={SAMPLE_SOURCE_ID}" in ai.prompts[1]
    # Every stored document stays on the sheet; the trim is only what the model was shown.
    assert [s.id for s in sheet.sources] == [SAMPLE_SOURCE_ID, *(f"extra-{i}" for i in range(5))]


def test_a_second_rejection_propagates_to_the_caller():
    ai = RejectingAI(DRAFT, failures=2)
    with pytest.raises(AIRequestRejected, match="131072"):
        structure_sheet(ai, "reason", SAMPLE.hospital, [(SAMPLE.sources[0], LONG_TEXT)], TODAY)
    assert len(ai.prompts) == 2  # once in full, once smaller, never a third time


def test_a_rejection_that_a_smaller_prompt_cannot_change_is_not_retried():
    """One short document already fits the retry budget, so the smaller prompt would be the
    same bytes: the refusal was not about length (an unsupported field, say) and the same
    request would be refused again. The first error propagates and no second call is made."""
    ai = RejectingAI(DRAFT, failures=1)
    with pytest.raises(AIRequestRejected, match="131072"):
        structure_sheet(ai, "reason", SAMPLE.hospital, SOURCES, TODAY)
    assert len(ai.prompts) == 1
    full = build_messages(SAMPLE.hospital, SOURCES)
    smaller = build_messages(SAMPLE.hospital, SOURCES, limit=RETRY_DOC_CHARS, head=RETRY_HEAD_CHARS)
    assert smaller == full and ai.prompts[0] == full[1]["content"]
