"""Senior flow: camera → read-back → two questions → result (spec §9, §11)."""

from decimal import Decimal

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from waive.ai.client import ZDRRequired
from waive.cases.images import ImageError
from waive.cases.service import (
    authorize,
    confirm_bill,
    set_household,
    submit_bill,
    submit_income_letter,
    view,
)
from waive.db import session_scope
from waive.learning.intake import ingest_paper
from waive.web.deps import deps_of, render

router = APIRouter()


def senior_links(token: str) -> dict[str, str]:
    base = f"/s/{token}"
    return {
        "start": base,
        "bill": f"{base}/bill",
        "confirm": f"{base}/confirm",
        "household": f"{base}/household",
        "income": f"{base}/income",
        "result": f"{base}/result",
        "paper": f"{base}/paper",
    }


def money(value: Decimal | None) -> str:
    return "" if value is None else f"${value:,.2f}"


def long_date(value) -> str:
    return "" if value is None else f"{value:%B} {value.day}, {value:%Y}"


def _page(request: Request, name: str, token: str, shown, **extra) -> HTMLResponse:
    return render(
        request,
        name,
        links=senior_links(token),
        case=shown,
        money=money,
        long_date=long_date,
        **extra,
    )


@router.get("/s/{token}", response_class=HTMLResponse)
def senior_start(request: Request, token: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        shown = view(ctx, row.id)
        if shown.tier is not None and shown.status in {"evaluated", "approved"}:
            return _page(request, "senior_result.html", token, shown)
        return _page(request, "senior_start.html", token, shown)


@router.post("/s/{token}/bill", response_class=HTMLResponse)
async def senior_bill(
    request: Request,
    token: str,
    photo: UploadFile = File(...),  # noqa: B008
) -> HTMLResponse:
    deps = deps_of(request)
    data = await photo.read()
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        if deps.ai is None:
            return _page(
                request,
                "senior_wait.html",
                token,
                view(ctx, row.id),
                reason="reading is switched off right now",
            )
        try:
            shown = submit_bill(ctx, row.id, data)
        except ImageError:
            return _page(
                request,
                "senior_start.html",
                token,
                view(ctx, row.id),
                problem="We could not read that photo. Please try again in good light.",
            )
        except ZDRRequired:
            return _page(
                request,
                "senior_wait.html",
                token,
                view(ctx, row.id),
                reason="your helper needs to finish setting things up",
            )
        return _page(request, "senior_readback.html", token, shown)


@router.post("/s/{token}/confirm")
def senior_confirm(request: Request, token: str, answer: str = Form(...)):
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        if answer == "yes":
            confirm_bill(ctx, row.id, {})
            return RedirectResponse(senior_links(token)["household"], status_code=303)
        return _page(
            request,
            "senior_wait.html",
            token,
            view(ctx, row.id),
            reason="your helper will fix the details",
        )


@router.get("/s/{token}/household", response_class=HTMLResponse)
def senior_household_form(request: Request, token: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        return _page(request, "senior_household.html", token, view(ctx, row.id))


@router.post("/s/{token}/household")
def senior_household(
    request: Request, token: str, size: int = Form(...), programs: str = Form("none")
):
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        shown = view(ctx, row.id)
        chosen = () if programs == "none" else tuple(programs.split(","))
        set_household(ctx, row.id, size, shown.annual_income, chosen)
    return RedirectResponse(senior_links(token)["income"], status_code=303)


@router.get("/s/{token}/income", response_class=HTMLResponse)
def senior_income_form(request: Request, token: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        return _page(request, "senior_income.html", token, view(ctx, row.id))


@router.post("/s/{token}/income")
async def senior_income(
    request: Request,
    token: str,
    photo: UploadFile | None = File(None),  # noqa: B008
    skip: str | None = Form(None),
):
    deps = deps_of(request)
    data = await photo.read() if photo is not None and photo.filename else b""
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        if data and deps.ai is not None:
            try:
                submit_income_letter(ctx, row.id, data)
            except (ImageError, ZDRRequired):
                return _page(
                    request,
                    "senior_income.html",
                    token,
                    view(ctx, row.id),
                    problem="We could not read that letter. You can skip this step.",
                )
    return RedirectResponse(senior_links(token)["result"], status_code=303)


@router.get("/s/{token}/result", response_class=HTMLResponse)
def senior_result(request: Request, token: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        shown = view(ctx, row.id)
        if shown.tier is None:
            return _page(
                request,
                "senior_wait.html",
                token,
                shown,
                reason="your helper will finish the check",
            )
        return _page(request, "senior_result.html", token, shown)


@router.post("/s/{token}/paper", response_class=HTMLResponse)
async def senior_paper(
    request: Request,
    token: str,
    photo: UploadFile = File(...),  # noqa: B008
) -> HTMLResponse:
    deps = deps_of(request)
    data = await photo.read()
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        if deps.ai is None:
            return _page(
                request,
                "senior_wait.html",
                token,
                view(ctx, row.id),
                reason="reading is switched off right now",
            )
        try:
            result = ingest_paper(ctx, row.id, data)
        except ImageError:
            return _page(
                request,
                "senior_paper.html",
                token,
                view(ctx, row.id),
                message="We could not read that photo. Please try again in good light.",
            )
        except ZDRRequired:
            return _page(
                request,
                "senior_wait.html",
                token,
                view(ctx, row.id),
                reason="your helper needs to finish setting things up",
            )
        return _page(request, "senior_paper.html", token, view(ctx, row.id), message=result.message)
