"""Public atlas metrics (spec §16): counts, ages, accuracy and credits — never personal data."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from waive.atlas.metrics import Metrics, atlas_metrics, metrics_json
from waive.db import session_scope
from waive.web.deps import deps_of, render

router = APIRouter()


def _metrics(request: Request) -> Metrics:
    deps = deps_of(request)
    settings = request.app.state.settings
    with session_scope(deps.engine) as session:
        return atlas_metrics(
            session, deps.today_fn(), request.app.state.governor, settings.scout_daily_credits
        )


@router.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: Request) -> HTMLResponse:
    return render(request, "metrics.html", m=_metrics(request))


@router.get("/metrics.json")
def metrics_data(request: Request) -> JSONResponse:
    return JSONResponse(metrics_json(_metrics(request)))
