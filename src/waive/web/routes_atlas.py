"""Public, read-only atlas pages (spec §7: open, cited, versioned)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from waive.atlas import repo
from waive.db import session_scope
from waive.learning.evidence import public_flag_level, slip_cases
from waive.web.deps import deps_of, render

router = APIRouter()


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
            rows.append(
                {
                    "ccn": hospital.ccn,
                    "name": hospital.name,
                    "city": hospital.city,
                    "state": hospital.state,
                    "status": found[0].status.value if found else "none",
                    "version": found[0].version if found else None,
                }
            )
    return render(request, "atlas_list.html", rows=rows, q=q)


@router.get("/atlas/{ccn}.json")
def atlas_json(request: Request, ccn: str) -> JSONResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        found = repo.latest_sheet(session, ccn)
        if found is None:
            raise KeyError(ccn)
        return JSONResponse(found[0].model_dump(mode="json"))


@router.get("/atlas/{ccn}", response_class=HTMLResponse)
def atlas_sheet(request: Request, ccn: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        found = repo.latest_sheet(session, ccn)
        if found is None:
            raise KeyError(ccn)
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
        )
