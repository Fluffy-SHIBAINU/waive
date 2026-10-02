import json
import random
from decimal import Decimal

from waive.cases.images import prepare_image
from waive.cases.synth import BillTruth, generate_corpus, make_truth, render_bill


def test_make_truth_is_deterministic_and_fictional():
    a, b = make_truth(random.Random(1)), make_truth(random.Random(1))
    assert a == b
    assert a.fap_phone.startswith("617-555-01")
    host = a.fap_url.split("//")[-1].split("/")[0]  # truth URLs are printed without a scheme
    assert host.endswith((".example.org", "example.org", "example.net"))
    assert isinstance(a.amount_due, Decimal) and a.amount_due > 0


def test_render_bill_produces_readable_jpeg():
    truth = make_truth(random.Random(2))
    jpeg = render_bill(truth, random.Random(2), layout=0, rotate_deg=2.0, blur=0.0)
    prepared = prepare_image(jpeg)
    assert prepared.warnings == ()
    assert prepared.width >= 1200


def test_generate_corpus_writes_pairs(tmp_path):
    paths = generate_corpus(tmp_path, count=4, seed=3)
    assert len(paths) == 4
    truth = json.loads((tmp_path / "bill-000.json").read_text())
    assert set(truth) >= {"hospital_name", "amount_due", "statement_date", "fap_url"}
    assert (tmp_path / "bill-000.jpg").stat().st_size > 20_000
    generate_corpus(tmp_path / "again", count=4, seed=3)
    assert (tmp_path / "again" / "bill-000.json").read_text() == (
        tmp_path / "bill-000.json"
    ).read_text()
    assert BillTruth.model_validate(truth).amount_due == Decimal(truth["amount_due"])
