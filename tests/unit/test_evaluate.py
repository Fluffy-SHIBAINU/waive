import random
from datetime import date
from decimal import Decimal
from pathlib import Path

from waive.cases.evaluate import compare, evaluate_corpus, write_report
from waive.cases.extract import BillExtract
from waive.cases.synth import generate_corpus, make_truth


def test_compare_is_tolerant_where_it_should_be():
    truth = make_truth(random.Random(5))
    extract = BillExtract(
        hospital_name=truth.hospital_name.upper().replace("CENTER", "CTR"),
        statement_date=truth.statement_date,
        amount_due=truth.amount_due,
        fap_phone=f"({truth.fap_phone[:3]}) {truth.fap_phone[4:]}",
        fap_url="https://" + truth.fap_url,
        collection_notice=truth.collection_notice,
    )
    assert all(compare(truth, extract).values())
    wrong = BillExtract(
        amount_due=Decimal("1"),
        statement_date=date(2000, 1, 1),
        collection_notice=not truth.collection_notice,  # the default flag would match a False truth
    )
    assert not any(compare(truth, wrong).values())


class PerfectAI:
    def __init__(self, corpus_dir):
        self.corpus_dir = Path(corpus_dir)
        self.index = 0

    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        import json

        truth = json.loads((self.corpus_dir / f"bill-{self.index:03d}.json").read_text())
        self.index += 1
        return schema.model_validate({**truth, "hospital_phone": truth["fap_phone"]})


def test_evaluate_corpus_and_report(tmp_path):
    generate_corpus(tmp_path / "corpus", count=3, seed=9)
    scores = evaluate_corpus(PerfectAI(tmp_path / "corpus"), tmp_path / "corpus")
    assert scores["n"] == 3 and scores["amount_due"] == 1.0
    write_report(scores, tmp_path / "report.md")
    text = (tmp_path / "report.md").read_text()
    assert "| amount_due | 100% |" in text
