"""Route a photo of a hospital paper to the right handler (spec §10)."""

from dataclasses import dataclass

from waive.ai.client import AIOutputError
from waive.cases.images import prepare_image
from waive.cases.service import CaseContext, get_row, load_sealed, save_sealed
from waive.learning.classify import (
    PhotoClass,
    PhotoClassification,
    Route,
    classify_photo,
    route_for,
)
from waive.learning.contributions import submit_contribution

MESSAGES: dict[Route, str] = {
    "bill": "That looks like a bill. Use the bill step for it.",
    "income": "That looks like a benefit letter. Use the income step for it.",
    "outcome": "Thank you. We will read the hospital's answer.",
    "contribution": "Thank you. This paper helps other patients too; a reviewer checks it first.",
    "ignore": "We could not tell what this paper is. Your helper can look at it.",
}


@dataclass(frozen=True)
class PaperResult:
    photo_class: PhotoClass
    route: Route
    message: str


def ingest_paper(
    ctx: CaseContext, case_id: str, image_bytes: bytes, *, synthetic: bool = False
) -> PaperResult:
    row = get_row(ctx, case_id)
    prepared = prepare_image(image_bytes)
    try:
        classified = classify_photo(ctx.ai, prepared.jpeg, synthetic=synthetic)
    except AIOutputError:
        classified = PhotoClassification()
    route = route_for(classified.photo_class)
    sealed = load_sealed(ctx, row)
    sealed.setdefault("papers", []).append(
        {"photo_class": classified.photo_class.value, "route": route, "on": ctx.today.isoformat()}
    )
    save_sealed(ctx, row, sealed)
    if route == "contribution":
        submit_contribution(
            ctx.session,
            ccn=row.ccn,
            case_id=row.id,
            photo_class=classified.photo_class,
            text=classified.transcription,
            vision_flag=classified.personal_info,
            today=ctx.today,
        )
    return PaperResult(classified.photo_class, route, MESSAGES[route])
