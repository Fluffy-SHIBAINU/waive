import base64
from datetime import date
from decimal import Decimal

from waive.cases.extract import (
    BILL_PROMPT,
    BillExtract,
    IncomeExtract,
    extract_bill,
    extract_income,
    image_message,
)


class FakeAI:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        self.calls.append({"role": role, "messages": messages, "phi": phi, "purpose": purpose})
        return schema.model_validate(self.payload)


JPEG = b"\xff\xd8\xff\xe0fake"


def test_image_message_embeds_base64_jpeg():
    message = image_message(JPEG, "Read this.")
    assert message["role"] == "user"
    assert message["content"][0] == {"type": "text", "text": "Read this."}
    url = message["content"][1]["image_url"]["url"]
    assert url == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


def test_extract_bill_uses_vision_with_phi_flag():
    ai = FakeAI(
        {
            "hospital_name": "St. Example Medical Center",
            "amount_due": "1850.00",
            "statement_date": "2026-09-03",
            "fap_url": "www.example.org/financial-assistance",
        }
    )
    result = extract_bill(ai, JPEG)
    assert result == BillExtract(
        hospital_name="St. Example Medical Center",
        amount_due=Decimal("1850.00"),
        statement_date=date(2026, 9, 3),
        fap_url="www.example.org/financial-assistance",
    )
    call = ai.calls[0]
    assert (call["role"], call["phi"], call["purpose"]) == ("vision", True, "case.bill")
    assert call["messages"][0]["role"] == "system"
    assert call["messages"][1]["content"][1]["type"] == "image_url"


def test_bill_prompt_asks_whether_this_is_the_first_statement():
    # 501(r) clocks run from the first post-discharge statement; the model must say which one
    # it is reading, and the caregiver can supply the first bill's date separately.
    assert "is_first_statement" in BILL_PROMPT
    for cue in ("previous balance", "past due", "final notice"):
        assert cue in BILL_PROMPT.lower()
    extract = BillExtract(statement_date=date(2026, 9, 3), first_statement_date=date(2026, 7, 5))
    assert extract.first_statement_date == date(2026, 7, 5)
    assert BillExtract().first_statement_date is None


def test_synthetic_bills_do_not_set_phi():
    ai = FakeAI({})
    extract_bill(ai, JPEG, synthetic=True)
    assert ai.calls[0]["phi"] is False


def test_income_extract_annualizes_monthly_benefit():
    ai = FakeAI({"monthly_benefit": "1900", "benefit_year": 2026})
    result = extract_income(ai, JPEG)
    assert result.annual() == Decimal("22800")
    assert ai.calls[0]["purpose"] == "case.income"
    assert IncomeExtract(annual_income=Decimal("30000")).annual() == Decimal("30000")
    assert IncomeExtract().annual() is None
