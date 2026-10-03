from decimal import Decimal

from waive.atlas.samples import st_example_sheet
from waive.atlas.schema import DiscountTier, DocType, StateProgram, SubmitMethod
from waive.web.plain import plain_lines


def test_fpl_limit_reads_as_a_share_of_the_poverty_level():
    path = "eligibility.free_care_max_fpl"
    assert plain_lines(Decimal("250"), path) == ["250% of the federal poverty level"]
    # No trailing zeros and no exponent, whatever the Decimal's internal form.
    assert plain_lines(Decimal("137.50"), path) == ["137.5% of the federal poverty level"]
    assert plain_lines(Decimal("1E+2"), path) == ["100% of the federal poverty level"]


def test_money_reads_as_dollars_and_cents():
    assert plain_lines(Decimal("1234"), "eligibility.min_balance") == ["$1,234.00"]
    assert plain_lines(Decimal("50.5"), "eligibility.min_balance") == ["$50.50"]


def test_discount_tiers_one_line_per_tier():
    tiers = [
        DiscountTier(
            min_fpl_exclusive=Decimal("200"), max_fpl_inclusive=Decimal("300"), discount_percent=60
        ),
        DiscountTier(
            min_fpl_exclusive=Decimal("300"), max_fpl_inclusive=Decimal("400"), discount_percent=40
        ),
    ]
    assert plain_lines(tiers, "eligibility.discount_tiers") == [
        "Above 200% up to 300% of FPL: 60% discount",
        "Above 300% up to 400% of FPL: 40% discount",
    ]


def test_state_programs_name_then_how_to_apply():
    programs = [
        StateProgram(
            name="Health Safety Net (Massachusetts)",
            how_to_apply="Apply through the MassHealth application.",
        )
    ]
    assert plain_lines(programs, "programs.state_programs") == [
        "Health Safety Net (Massachusetts) — Apply through the MassHealth application."
    ]


def test_submit_methods_capitalise_the_kind():
    methods = [
        SubmitMethod(kind="mail", detail="1 Example Way, Boston, MA 02118"),
        SubmitMethod(kind="fax", detail="617-555-0199"),
        SubmitMethod(kind="in_person", detail="Patient Financial Services, 2nd floor"),
    ]
    assert plain_lines(methods, "apply.submit_methods") == [
        "Mail: 1 Example Way, Boston, MA 02118",
        "Fax: 617-555-0199",
        "In person: Patient Financial Services, 2nd floor",
    ]


def test_every_document_type_has_a_readable_label():
    assert plain_lines(list(DocType), "apply.documents_required") == [
        "Photo ID",
        "Proof of income",
        "Social Security letter",
        "Tax return",
        "Pay stubs",
        "Bank statements",
        "Proof of residency",
        "Insurance card",
        "Medicaid denial letter",
        "Other documents",
    ]
    # The reported-documents field uses the same labels.
    assert plain_lines([DocType.PAY_STUBS], "apply.documents_reported") == ["Pay stubs"]


def test_repeated_items_are_listed_once():
    docs = [DocType.OTHER, DocType.PHOTO_ID, DocType.OTHER]
    assert plain_lines(docs, "apply.documents_required") == ["Other documents", "Photo ID"]


def test_booleans_read_yes_or_no():
    assert plain_lines(True, "eligibility.asset_test") == ["Yes"]
    assert plain_lines(False, "eligibility.insured_patients_covered") == ["No"]


def test_day_counts_say_days():
    assert plain_lines(240, "apply.window_days_from_first_bill") == ["240 days"]
    assert plain_lines(30, "apply.decision_days") == ["30 days"]
    assert plain_lines(45, "apply.decision_days_reported") == ["45 days"]
    assert plain_lines(1, "collections.eca_wait_days") == ["1 day"]


def test_other_integers_stay_plain_numbers():
    assert plain_lines(7, "some.count") == ["7"]


def test_string_lists_one_item_per_line():
    assert plain_lines(["MassHealth", "SNAP"], "programs.presumptive") == ["MassHealth", "SNAP"]
    assert plain_lines(["English", "Spanish"], "contacts.languages") == ["English", "Spanish"]


def test_strings_are_unchanged():
    assert plain_lines("617-555-0100", "contacts.phone") == ["617-555-0100"]
    url = "https://example.org/form.pdf"
    assert plain_lines(url, "apply.form_url") == [url]


def test_empty_and_unknown_values():
    assert plain_lines([], "programs.presumptive") == []
    assert plain_lines(None, "contacts.phone") == []
    assert plain_lines(1.5, "some.ratio") == ["1.5"]


def test_the_sample_sheet_renders_without_repr_markers():
    for path, cited in st_example_sheet().field_paths():
        lines = plain_lines(cited.value, path)
        assert lines, path
        for line in lines:
            assert "[" not in line and "]" not in line, (path, line)
            assert "=" not in line and "DocType" not in line and "Decimal" not in line, (path, line)
