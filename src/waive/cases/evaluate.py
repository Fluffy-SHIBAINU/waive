"""Per-field extraction accuracy on the synthetic corpus (spec §13)."""

import json
import re
from pathlib import Path

from rapidfuzz import fuzz

from waive.ai.client import AIClient
from waive.cases.extract import BillExtract, extract_bill
from waive.cases.synth import BillTruth

FIELDS = (
    "hospital_name",
    "statement_date",
    "amount_due",
    "fap_phone",
    "fap_url",
    "collection_notice",
)


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _url(value: str | None) -> str:
    text = (value or "").lower().strip()
    text = re.sub(r"^https?://", "", text).removeprefix("www.")
    return text.rstrip("/")


def compare(truth: BillTruth, extract: BillExtract) -> dict[str, bool]:
    return {
        "hospital_name": fuzz.token_set_ratio(
            (extract.hospital_name or "").lower().replace("ctr", "center"),
            truth.hospital_name.lower(),
        )
        >= 90,
        "statement_date": extract.statement_date == truth.statement_date,
        "amount_due": extract.amount_due is not None and extract.amount_due == truth.amount_due,
        "fap_phone": _digits(extract.fap_phone) == _digits(truth.fap_phone),
        "fap_url": _url(extract.fap_url) == _url(truth.fap_url),
        "collection_notice": extract.collection_notice == truth.collection_notice,
    }


def evaluate_corpus(ai: AIClient, corpus_dir: Path, limit: int | None = None) -> dict[str, float]:
    images = sorted(Path(corpus_dir).glob("bill-*.jpg"))[:limit]
    hits = dict.fromkeys(FIELDS, 0)
    for image in images:
        truth = BillTruth.model_validate(json.loads(image.with_suffix(".json").read_text()))
        extract = extract_bill(ai, image.read_bytes(), synthetic=True)
        for field_name, ok in compare(truth, extract).items():
            hits[field_name] += int(ok)
    n = len(images)
    scores: dict[str, float] = {
        field_name: (hits[field_name] / n if n else 0.0) for field_name in FIELDS
    }
    scores["n"] = float(n)
    return scores


def write_report(scores: dict[str, float], path: Path) -> None:
    lines = [
        "# Bill extraction accuracy (synthetic corpus)",
        "",
        f"Bills: {int(scores['n'])}",
        "",
        "| Field | Accuracy |",
        "|---|---|",
    ]
    lines += [f"| {field_name} | {scores[field_name]:.0%} |" for field_name in FIELDS]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
