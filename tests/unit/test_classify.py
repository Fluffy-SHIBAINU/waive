from waive.learning.classify import (
    OUTCOME_CLASSES,
    PUBLIC_CLASSES,
    PhotoClass,
    PhotoClassification,
    classify_photo,
    route_for,
)


class FakeAI:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        self.calls.append({"role": role, "messages": messages, "phi": phi, "purpose": purpose})
        return schema.model_validate(self.payload)


JPEG = b"\xff\xd8\xff\xe0fake"


def test_classes_match_the_spec_list():
    assert [c.value for c in PhotoClass] == [
        "bill",
        "eob",
        "decision_letter",
        "information_request",
        "plain_language_summary",
        "fap",
        "application_form",
        "social_security_letter",
        "other",
    ]
    assert PUBLIC_CLASSES == {
        PhotoClass.PLAIN_LANGUAGE_SUMMARY,
        PhotoClass.FAP,
        PhotoClass.APPLICATION_FORM,
    }
    assert OUTCOME_CLASSES == {PhotoClass.DECISION_LETTER, PhotoClass.INFORMATION_REQUEST}


def test_classify_uses_vision_role_with_phi_and_transcription():
    ai = FakeAI(
        {
            "photo_class": "fap",
            "confidence": 0.8,
            "personal_info": False,
            "transcription": "Policy text.",
        }
    )
    result = classify_photo(ai, JPEG)
    assert result == PhotoClassification(
        photo_class=PhotoClass.FAP, confidence=0.8, transcription="Policy text."
    )
    call = ai.calls[0]
    assert (call["role"], call["phi"], call["purpose"]) == ("vision", True, "learn.classify")
    assert (
        call["messages"][0]["role"] == "system" and "photo_class" in call["messages"][0]["content"]
    )
    assert call["messages"][1]["content"][1]["type"] == "image_url"
    assert classify_photo(FakeAI({}), JPEG, synthetic=True).photo_class is PhotoClass.OTHER
    assert FakeAI({}).calls == []


def test_routes_by_class():
    assert route_for(PhotoClass.BILL) == "bill"
    assert route_for(PhotoClass.SOCIAL_SECURITY_LETTER) == "income"
    assert (
        route_for(PhotoClass.DECISION_LETTER)
        == route_for(PhotoClass.INFORMATION_REQUEST)
        == "outcome"
    )
    assert route_for(PhotoClass.FAP) == route_for(PhotoClass.APPLICATION_FORM) == "contribution"
    assert route_for(PhotoClass.EOB) == route_for(PhotoClass.OTHER) == "ignore"
