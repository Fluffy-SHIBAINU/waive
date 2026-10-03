"""FastAPI application factory."""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from waive.ai.client import AIClient
from waive.cases.vault import cipher_from_settings, signer_from_settings
from waive.config import Settings
from waive.db import init_db, make_engine
from waive.governor import make_governor
from waive.logging_setup import configure_logging
from waive.web.deps import STATIC_DIR, TEMPLATES_DIR, Deps, render, today_utc


def _default_ai(settings: Settings):
    if settings.nebius_api_key is None:
        return None
    return AIClient(settings, make_governor(settings))


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
    app = FastAPI(title="Waive", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.deps = Deps(
        engine=engine,
        ai=ai if ai is not None else _default_ai(settings),
        cipher=cipher or cipher_from_settings(settings),
        signer=signer or signer_from_settings(settings),
        today_fn=today_fn or today_utc,
        templates=Jinja2Templates(directory=str(TEMPLATES_DIR)),
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from waive.web import routes_admin, routes_atlas, routes_caregiver, routes_senior

    app.include_router(routes_senior.router)
    app.include_router(routes_caregiver.router)
    app.include_router(routes_atlas.router)
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

    return app
