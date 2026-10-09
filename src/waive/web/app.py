"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.formparsers import MultiPartParser

from waive.ai.client import AIClient
from waive.cases.vault import cipher_from_settings, signer_from_settings
from waive.config import Settings
from waive.db import init_db, make_engine
from waive.governor import make_governor
from waive.logging_setup import configure_logging
from waive.web.deps import STATIC_DIR, TEMPLATES_DIR, Deps, render, today_utc
from waive.web.limits import BodyLimit, UploadTooLarge
from waive.web.plain import plain_lines


def _default_ai(settings: Settings, governor):
    if settings.nebius_api_key is None:
        return None
    return AIClient(settings, governor)


def create_app(
    settings: Settings | None = None,
    *,
    engine=None,
    ai=None,
    cipher=None,
    signer=None,
    today_fn=None,
) -> FastAPI:
    configure_logging()
    settings = settings or Settings()
    engine = engine or make_engine(settings.database_url)
    init_db(engine)
    governor = make_governor(settings, engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Phase 7: the scouting job lives inside the app only when WAIVE_SCHEDULER=on; it is off
        # by default so a test client or a developer's `waive serve` never spends credits.
        scheduler = None
        if settings.scheduler == "on":
            from waive.atlas.schedule import make_scheduler

            scheduler = make_scheduler(engine, settings, governor, app.state.deps.ai)
            scheduler.start()
        app.state.scheduler = scheduler
        yield
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="Waive", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.governor = governor
    app.add_middleware(BodyLimit, max_body=settings.max_upload_bytes)
    # Starlette spools multipart files over 1 MiB to a plaintext temp file. With the cap in front
    # nothing admitted needs to leave memory, which keeps "photos are never written to disk" true.
    MultiPartParser.spool_max_size = settings.max_upload_bytes + 1
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # `{{ cited.value | plain(path) }}`: cited atlas values in plain language (atlas_sheet.html).
    templates.env.filters["plain"] = plain_lines
    app.state.deps = Deps(
        engine=engine,
        ai=ai if ai is not None else _default_ai(settings, governor),
        cipher=cipher or cipher_from_settings(settings),
        signer=signer or signer_from_settings(settings),
        today_fn=today_fn or today_utc,
        templates=templates,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from waive.web import (
        routes_admin,
        routes_atlas,
        routes_caregiver,
        routes_metrics,
        routes_senior,
    )

    app.include_router(routes_senior.router)
    app.include_router(routes_caregiver.router)
    app.include_router(routes_atlas.router)
    app.include_router(routes_metrics.router)
    app.include_router(routes_admin.router)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> HTMLResponse:
        return render(request, "home.html")

    @app.exception_handler(PermissionError)
    def forbidden(request: Request, exc: PermissionError) -> HTMLResponse:
        return render(
            request,
            "error.html",
            status_code=403,
            title="This link is not valid",
            message="The link is not valid or has expired. Ask your helper for a new one.",
        )

    @app.exception_handler(KeyError)
    def not_found(request: Request, exc: KeyError) -> HTMLResponse:
        return render(
            request,
            "error.html",
            status_code=404,
            title="Not found",
            message="We could not find that page.",
        )

    @app.exception_handler(UploadTooLarge)
    def too_large(request: Request, exc: UploadTooLarge) -> HTMLResponse:
        return render(
            request,
            "error.html",
            status_code=413,
            title="That photo is too large",
            message="Please take a smaller photo and try again.",
        )

    return app
