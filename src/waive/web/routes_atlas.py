"""Public, read-only atlas pages (spec §7: open, cited, versioned).

Only published sheets are shown (audit rules-correctness-9): a hospital whose newest version is
held is "under review", with no figures, until an admin publishes it again."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from waive.atlas import repo
from waive.atlas.schema import SheetStatus
from waive.db import HospitalRow, session_scope
from waive.learning.evidence import public_flag_level, slip_cases
from waive.ledger import web as ledger_web
from waive.ledger.view import ledger_json, ledger_view
from waive.web.deps import deps_of, render

router = APIRouter()
UNDER_REVIEW = "under_review"


def _under_review(hospital: HospitalRow) -> dict[str, str]:
    """What the public sees of a hospital whose sheet is held: who it is, and that it waits."""
    return {
        "ccn": hospital.ccn,
        "name": hospital.name,
        "city": hospital.city,
        "state": hospital.state,
        "status": UNDER_REVIEW,
    }


@router.get("/atlas", response_class=HTMLResponse)
def atlas_list(request: Request, q: str = "", demo: bool = False) -> HTMLResponse:
    """List hospitals; the fictional demo hospital only appears with `?demo=1`."""
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        rows = []
        for hospital in repo.list_hospitals(session):
            if repo.is_demo(hospital.ccn) and not demo:
                continue
            if q and q.lower() not in f"{hospital.name} {hospital.city}".lower():
                continue
            found = repo.latest_sheet(session, hospital.ccn)
            published = found is not None and found[0].status is SheetStatus.PUBLISHED
            if found is None:
                status = "none"
            else:
                status = "published" if published else "under review"
            rows.append(
                {
                    "ccn": hospital.ccn,
                    "name": hospital.name,
                    "city": hospital.city,
                    "state": hospital.state,
                    "status": status,
                    "version": found[0].version if published else None,
                }
            )
    return render(request, "atlas_list.html", rows=rows, q=q)


@router.get("/atlas/{ccn}.json")
def atlas_json(request: Request, ccn: str) -> JSONResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        found = repo.published_sheet(session, ccn)
        if found is not None:
            return JSONResponse(
                {**found[0].model_dump(mode="json"), "ledger": ledger_json(session, ccn)}
            )
        hospital = repo.get_hospital(session, ccn)
        if hospital is None or repo.latest_sheet(session, ccn) is None:
            raise KeyError(ccn)
        return JSONResponse({**_under_review(hospital), "ledger": ledger_json(session, ccn)})


@router.get("/atlas/{ccn}", response_class=HTMLResponse)
def atlas_sheet(request: Request, ccn: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        found = repo.published_sheet(session, ccn)
        if found is None:
            hospital = repo.get_hospital(session, ccn)
            if hospital is None or repo.latest_sheet(session, ccn) is None:
                raise KeyError(ccn)
            return render(
                request,
                "atlas_under_review.html",
                hospital=_under_review(hospital),
                ledger=ledger_view(session, ccn),
            )
        sheet, row = found
        sources = {s.id: s for s in sheet.sources}
        fields = [
            (path, cited, sources.get(cited.source_id)) for path, cited in sheet.field_paths()
        ]
        return render(
            request,
            "atlas_sheet.html",
            sheet=sheet,
            fields=fields,
            created=row.created_at,
            flag=public_flag_level(session, ccn),
            slip_count=slip_cases(session, ccn),
            ledger=ledger_view(session, ccn),
        )


router.include_router(ledger_web.router)
