"""Deterministic fictional hospital bills for tests and accuracy reports (spec §13)."""

import io
import json
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from pydantic import BaseModel

FICTIONAL_HOSPITALS = [
    ("St. Example Medical Center", "617-555-0100", "www.example.org/financial-assistance"),
    ("Northbridge Community Hospital", "617-555-0110", "www.northbridge.example.org/billing-help"),
    ("Harbor General Hospital", "617-555-0120", "harbor.example.net/financial-assistance"),
    (
        "Pioneer Valley Regional Medical Center",
        "617-555-0130",
        "www.pioneervalley.example.org/help-paying",
    ),
    (
        "Blue Hills Memorial Hospital",
        "617-555-0140",
        "www.bluehills.example.org/financial-assistance",
    ),
    ("Cape Example Hospital", "617-555-0150", "capeexample.example.net/patient-financial-services"),
]
FIRST_NAMES = ["Rosa", "Miguel", "Agnes", "Walter", "Thuy", "Dorothy", "Samuel", "Irene"]
LAST_NAMES = ["Alvarez", "Nguyen", "Kowalski", "Okafor", "Brennan", "Haddad", "Lindqvist", "Patel"]


class BillTruth(BaseModel):
    hospital_name: str
    hospital_phone: str
    fap_phone: str
    fap_url: str
    statement_date: date
    account_reference: str
    patient_name: str
    amount_due: Decimal
    collection_notice: bool


def make_truth(rng: random.Random) -> BillTruth:
    name, phone, url = rng.choice(FICTIONAL_HOSPITALS)
    dollars = rng.choice([185, 420, 975, 1850, 2340, 4120, 8400, 12650]) + rng.choice(
        [0, 0.5, 0.25]
    )
    return BillTruth(
        hospital_name=name,
        hospital_phone=phone,
        fap_phone=phone,
        fap_url=url,
        statement_date=date(2026, 1, 1) + timedelta(days=rng.randrange(0, 270)),
        account_reference=f"ACCT-{rng.randrange(10**7, 10**8)}",
        patient_name=f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
        amount_due=Decimal(str(dollars)).quantize(Decimal("0.01")),
        collection_notice=rng.random() < 0.2,
    )


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def render_bill(
    truth: BillTruth, rng: random.Random, layout: int, rotate_deg: float, blur: float
) -> bytes:
    width, height = 1700, 2200
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    big, body, small = _font(52), _font(34), _font(28)
    header_x = 100 if layout % 2 == 0 else 700
    draw.text((header_x, 90), truth.hospital_name, fill="black", font=big)
    draw.text((header_x, 160), "Patient Financial Services", fill="black", font=body)
    draw.text((header_x, 205), f"Phone: {truth.hospital_phone}", fill="black", font=body)
    draw.text((100, 330), "STATEMENT", fill="black", font=big)
    rows = [
        ("Statement date", truth.statement_date.strftime("%m/%d/%Y")),
        ("Account number", truth.account_reference),
        ("Patient", truth.patient_name),
        ("Insurance payments", "$0.00" if rng.random() < 0.5 else f"${rng.randrange(100, 900)}.00"),
        ("AMOUNT DUE", f"${truth.amount_due:,.2f}"),
    ]
    y = 430
    for label, value in rows:
        draw.text((100, y), label, fill="black", font=body)
        draw.text((900, y), value, fill="black", font=body)
        y += 70
    if layout == 2:
        draw.rectangle((90, 420, 1600, y), outline="black", width=3)
    notice = (
        "Financial assistance may be available. If you cannot afford this bill, call "
        f"{truth.fap_phone} or visit {truth.fap_url} for our financial assistance policy and application."
    )
    words, line, lines = notice.split(), "", []
    for word in words:
        if len(line) + len(word) > 70:
            lines.append(line)
            line = ""
        line = f"{line} {word}".strip()
    lines.append(line)
    y += 60
    for text in lines:
        draw.text((100, y), text, fill="black", font=small)
        y += 40
    if truth.collection_notice:
        draw.text(
            (100, y + 40),
            "FINAL NOTICE: this account may be referred to a collection agency.",
            fill="black",
            font=body,
        )
    if rotate_deg:
        image = image.rotate(rotate_deg, expand=True, fillcolor="white")
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def render_benefit_letter(name: str, monthly: Decimal, letter_date: date) -> bytes:
    """A fictional Social Security benefit-verification letter for demos and tests. Same page
    size as the bills so the phone flow's intake treats it the same way. Deterministic."""
    width, height = 1700, 2200
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    big, body = _font(52), _font(34)
    draw.text((100, 90), "Social Security Administration", fill="black", font=big)
    draw.text((100, 165), "Benefit Verification Letter", fill="black", font=body)
    draw.text((100, 215), f"Date: {letter_date.strftime('%B %d, %Y')}", fill="black", font=body)
    draw.text((100, 330), name, fill="black", font=body)
    paragraphs = [
        "You asked us for information from your record. The information that",
        "you requested is shown below. If you want anyone else to have this",
        "information, you may send them this letter.",
        "",
        "Information About Current Social Security Benefits",
        "",
        f"Your monthly Social Security benefit is ${monthly:,.2f}.",
        f"Monthly benefit amount: ${monthly:,.2f}",
        "",
        "Your benefit is paid on the third Wednesday of each month.",
        "",
        "This letter is for your records. It is not a bill. Do not send money.",
    ]
    y = 430
    for text in paragraphs:
        draw.text((100, y), text, fill="black", font=body)
        y += 60
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def generate_corpus(out_dir: Path, count: int, seed: int = 7) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    paths: list[Path] = []
    for index in range(count):
        truth = make_truth(rng)
        jpeg = render_bill(
            truth,
            rng,
            layout=index % 3,
            rotate_deg=rng.choice([0.0, 1.5, -2.0, 3.0]),
            blur=rng.choice([0.0, 0.0, 0.0, 0.8]),
        )
        stem = out_dir / f"bill-{index:03d}"
        stem.with_suffix(".jpg").write_bytes(jpeg)
        stem.with_suffix(".json").write_text(
            json.dumps(truth.model_dump(mode="json"), indent=1, sort_keys=True)
        )
        paths.append(stem.with_suffix(".jpg"))
    return paths
