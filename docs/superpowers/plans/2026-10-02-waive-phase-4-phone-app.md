# Waive Phase 4 — Phone Web App and Packet Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The senior and caregiver flows working in a phone browser, plus a printable application packet, calendar reminders and public atlas pages.

**Architecture:** One FastAPI app (`waive.web`) with server-rendered Jinja2 pages and plain HTML forms (no JavaScript framework; one small inline script for read-aloud and share). Routes are thin: they authorize a capability token, call `waive.cases.service`, and render. `create_app()` takes injected dependencies (engine, AI client, cipher, signer, clock) so tests use fakes with Starlette's `TestClient`. The packet is built with reportlab (pure Python); reminders are a generated `.ics` file.

**Tech Stack:** FastAPI, Jinja2, python-multipart, reportlab, uvicorn, Starlette TestClient, Playwright (E2E, optional).

**Spec:** `docs/superpowers/specs/2026-10-02-waive-design.md` (§9, §11, §13). Deviations recorded here: plain CSS instead of Tailwind, no HTMX, reportlab instead of WeasyPrint (no native libraries to install) — the spec's intent (fast, accessible, server-rendered pages; a printable packet) is unchanged.

## Global Constraints

- Senior pages: base font 20px, buttons at least 56px tall and full width, one question per page, plain words, "likely" wording, no jargon. The footer on every page says the app never asks for money, card numbers or bank logins.
- Senior links can read and upload; only caregiver links can correct, approve, download the packet or delete a case.
- Photos are read from the request body into memory and passed to the service; they are never written to disk.
- No personal data in URLs: tokens are opaque; corrections travel in POST bodies.
- Pages must work without JavaScript; JavaScript only adds read-aloud and the share button.
- Interfaces relied on: everything exported by `waive.cases.service` (`CaseContext, start_case, authorize, submit_bill, confirm_bill, set_household, submit_income_letter, approve, delete_case, view, CaseView`), `waive.cases.vault` (`cipher_from_settings, signer_from_settings`), `waive.atlas.repo`, `waive.rules.deadlines.Deadlines`, `AIClient`, `Settings`, `make_engine/init_db/session_scope`.

---

### Task 4.1: App skeleton, layout and styles

**Files:**
- Create: `src/waive/web/__init__.py`, `src/waive/web/app.py`, `src/waive/web/deps.py`, `src/waive/web/routes_senior.py`, `src/waive/web/routes_caregiver.py`, `src/waive/web/routes_atlas.py`, `src/waive/web/templates/base.html`, `src/waive/web/templates/home.html`, `src/waive/web/templates/error.html`, `src/waive/web/static/waive.css`
- Modify: `pyproject.toml` (add `python-multipart>=0.0.9`), `src/waive/cli.py` (add `waive serve`)
- Test: `tests/unit/test_web_app.py`

**Interfaces:**
- Produces: `create_app(settings: Settings | None = None, *, engine=None, ai=None, cipher=None, signer=None, today_fn=None) -> FastAPI`; `Deps` dataclass on `app.state.deps` with `engine, ai, cipher, signer, today_fn, templates` and `context(session) -> CaseContext`; `render(request, name, status_code=200, **context) -> HTMLResponse`; `GET /` (home with "Start a case" form), `GET /healthz` → `{"ok": true}`; error pages for 403 (`PermissionError`) and 404 (`KeyError`).

- [ ] **Step 1: Add the dependency**

Run: `uv add "python-multipart>=0.0.9"`

- [ ] **Step 2: Write the failing tests**

`tests/unit/test_web_app.py`:

```python
import base64

from fastapi.testclient import TestClient
from pydantic import SecretStr

from waive.cases.vault import FieldCipher, TokenSigner, new_key
from waive.config import Settings
from waive.db import init_db, make_engine
from waive.web.app import create_app


def make_client(ai=None):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    settings = Settings(_env_file=None, nebius_api_key=SecretStr("k"))
    app = create_app(
        settings,
        engine=engine,
        ai=ai,
        cipher=FieldCipher(base64.b64decode(new_key())),
        signer=TokenSigner("x" * 40),
    )
    return TestClient(app), engine


def test_home_and_health():
    client, _ = make_client()
    assert client.get("/healthz").json() == {"ok": True}
    page = client.get("/")
    assert page.status_code == 200
    assert "Start" in page.text and "never asks for money" in page.text
    assert 'lang="en"' in page.text


def test_static_css_and_bad_token_page():
    client, _ = make_client()
    assert client.get("/static/waive.css").status_code == 200
    missing = client.get("/s/not-a-real-token")
    assert missing.status_code == 403
    assert "not valid" in missing.text
```

- [ ] **Step 3: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_web_app.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.web'`

- [ ] **Step 4: Implement**

`src/waive/web/__init__.py`:

```python
"""Phone-first web app: senior flow, caregiver flow, public atlas."""
```

`src/waive/web/deps.py`:

```python
"""Per-app dependencies and helpers shared by the route modules."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.engine import Engine

from waive.cases.service import CaseContext
from waive.cases.vault import FieldCipher, TokenSigner

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def today_utc() -> date:
    return datetime.now(UTC).date()


@dataclass
class Deps:
    engine: Engine
    ai: Any
    cipher: FieldCipher
    signer: TokenSigner
    today_fn: Callable[[], date]
    templates: Jinja2Templates

    def context(self, session) -> CaseContext:
        return CaseContext(
            session=session, ai=self.ai, cipher=self.cipher, signer=self.signer, today=self.today_fn()
        )


def deps_of(request: Request) -> Deps:
    return request.app.state.deps


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    templates = deps_of(request).templates
    return templates.TemplateResponse(request, name, context, status_code=status_code)
```

`src/waive/web/app.py`:

```python
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

    from waive.web import routes_atlas, routes_caregiver, routes_senior

    app.include_router(routes_senior.router)
    app.include_router(routes_caregiver.router)
    app.include_router(routes_atlas.router)

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
            request, "error.html", status_code=404, title="Not found", message="We could not find that page."
        )

    return app
```

Routers for this task (tasks 4.2, 4.3 and 4.6 replace them). `routes_caregiver.py` and `routes_atlas.py`:

```python
from fastapi import APIRouter

router = APIRouter()
```

`routes_senior.py` (a placeholder that authorizes, so the 403 test passes):

```python
from fastapi import APIRouter, Request

from waive.cases.service import authorize
from waive.db import session_scope
from waive.web.deps import deps_of

router = APIRouter()


@router.get("/s/{token}")
def senior_home(request: Request, token: str):
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        authorize(deps.context(session), token, "senior")
    return {"ok": True}
```

`src/waive/web/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Waive{% endblock %}</title>
<link rel="stylesheet" href="/static/waive.css">
</head>
<body>
<header class="top"><a href="/" class="brand">Waive</a></header>
<main class="page">
{% block content %}{% endblock %}
</main>
<footer class="foot">
<p>Waive never asks for money, card numbers or bank logins.</p>
<p>Results are estimates. The hospital makes the final decision.</p>
</footer>
{% block scripts %}{% endblock %}
</body>
</html>
```

`src/waive/web/templates/home.html`:

```html
{% extends "base.html" %}
{% block title %}Waive — help with hospital bills{% endblock %}
{% block content %}
<h1>Hospital bills you may not have to pay</h1>
{% if notice %}<p class="card big">{{ notice }}</p>{% endif %}
<p class="lead">Nonprofit hospitals must offer free or discounted care to people with lower incomes. Waive checks a bill against the hospital's own policy and prepares the application.</p>
<form method="post" action="/cases" class="stack">
  <label for="state">Which state is the hospital in?</label>
  <select id="state" name="state" required>
    <option value="MA" selected>Massachusetts</option>
  </select>
  <button type="submit" class="btn primary">Start a case</button>
</form>
<p><a href="/atlas">Browse hospital policies</a></p>
{% endblock %}
```

`src/waive/web/templates/error.html`:

```html
{% extends "base.html" %}
{% block title %}{{ title }}{% endblock %}
{% block content %}
<h1>{{ title }}</h1>
<p class="lead">{{ message }}</p>
<p><a href="/" class="btn">Go to the start</a></p>
{% endblock %}
```

`src/waive/web/static/waive.css`:

```css
:root { --ink: #111; --paper: #fff; --muted: #444; --line: #bbb; --accent: #0b5cad; --accent-ink: #fff; --ok: #1b6e3a; --warn: #8a5a00; --bad: #a32d2d; --tint: #f3f6fa; }
* { box-sizing: border-box; }
html { font-size: 20px; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: var(--ink); background: var(--paper); line-height: 1.5; }
.top { padding: 12px 16px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; align-items: center; }
.brand { font-weight: 700; font-size: 1.2rem; color: var(--ink); text-decoration: none; }
.page { max-width: 640px; margin: 0 auto; padding: 16px; }
.foot { max-width: 640px; margin: 32px auto 0; padding: 16px; border-top: 1px solid var(--line); color: var(--muted); font-size: 0.85rem; }
h1 { font-size: 1.6rem; line-height: 1.25; margin: 8px 0 16px; }
h2 { font-size: 1.25rem; margin: 24px 0 8px; }
.lead { font-size: 1.1rem; }
.big { font-size: 1.35rem; line-height: 1.4; }
.stack { display: grid; gap: 16px; }
label { font-weight: 600; }
input, select, textarea { font: inherit; width: 100%; min-height: 56px; padding: 12px; border: 2px solid var(--line); border-radius: 10px; background: var(--paper); color: var(--ink); }
.btn { display: block; width: 100%; min-height: 56px; padding: 14px 18px; font: inherit; font-weight: 700; text-align: center; text-decoration: none; color: var(--ink); background: var(--tint); border: 2px solid var(--accent); border-radius: 12px; cursor: pointer; }
.btn.primary { background: var(--accent); color: var(--accent-ink); }
.btn.quiet { border-color: var(--line); background: var(--paper); }
.btn.danger { border-color: var(--bad); color: var(--bad); background: var(--paper); }
.choices { display: grid; gap: 12px; }
.choices .btn { text-align: left; }
.card { border: 2px solid var(--line); border-radius: 12px; padding: 16px; margin: 16px 0; background: var(--tint); }
.status-free { border-color: var(--ok); }
.status-discount { border-color: var(--accent); }
.status-not_eligible, .status-unknown, .status-needs_info { border-color: var(--warn); }
.quote { border-left: 4px solid var(--accent); margin: 8px 0; padding: 4px 12px; color: var(--muted); font-style: italic; }
.muted { color: var(--muted); }
.row { display: flex; justify-content: space-between; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--line); }
.row:last-child { border-bottom: 0; }
a { color: var(--accent); }
.visually-hidden { position: absolute; left: -9999px; }
@media (prefers-color-scheme: dark) { :root { --ink: #f2f2f2; --paper: #121212; --muted: #c8c8c8; --line: #555; --tint: #1e2530; --accent: #7fb6ff; --accent-ink: #061a33; } }
```

Add to `src/waive/cli.py`:

```python
@app.command()
def serve(host: str = typer.Option("0.0.0.0", "--host"), port: int = typer.Option(8000, "--port")) -> None:
    """Run the web app (phones on the same Wi-Fi can open http://<this-computer-ip>:8000)."""
    import uvicorn

    uvicorn.run("waive.web.app:create_app", host=host, port=port, factory=True)
```

- [ ] **Step 5: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_web_app.py -v`
Expected: 2 passed

- [ ] **Step 6: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add pyproject.toml uv.lock src/waive/web src/waive/cli.py tests/unit/test_web_app.py
git commit -m "feat: web app skeleton with senior-friendly layout and styles"
```

---

### Task 4.2: Senior flow

**Files:**
- Replace: `src/waive/web/routes_senior.py`; create templates `senior_start.html`, `senior_readback.html`, `senior_household.html`, `senior_income.html`, `senior_result.html`, `senior_wait.html`
- Test: `tests/unit/test_web_senior.py`

**Interfaces:**
- Routes (all under `/s/{token}`, senior scope): `GET /s/{token}` start (camera button) · `POST /s/{token}/bill` (multipart `photo`) → read-back · `POST /s/{token}/confirm` (`answer=yes|no`) → household or wait · `GET/POST /s/{token}/household` (`size`, `programs`) · `GET/POST /s/{token}/income` (multipart `photo`, or `skip=1`) · `GET /s/{token}/result`.
- Helpers: `senior_links(token) -> dict`, `money(Decimal | None) -> str`, `long_date(date | None) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_web_senior.py`:

```python
import random
import re
from datetime import date
from decimal import Decimal

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.cases.extract import BillExtract, IncomeExtract
from waive.cases.service import start_case
from waive.cases.synth import make_truth, render_bill
from waive.db import session_scope

from tests.unit.test_web_app import make_client

HOSPITAL = {
    "ccn": "229999",
    "name": "ST. EXAMPLE MEDICAL CENTER",
    "city": "BOSTON",
    "state": "MA",
    "zip": "02118",
    "phone": "617-555-0100",
    "hospital_type": "Acute Care Hospitals",
    "ownership": "Voluntary non-profit - Private",
    "website_domain": "example.org",
}


class FakeAI:
    def complete_json(self, role, messages, schema, *, phi, purpose, max_tokens=2000):
        if schema is BillExtract:
            return BillExtract(
                hospital_name="St. Example Medical Center",
                hospital_phone="617-555-0100",
                statement_date=date(2026, 9, 3),
                amount_due=Decimal("1850.00"),
                confidence=0.9,
            )
        return IncomeExtract(monthly_benefit=Decimal("1900"))


def seeded_client():
    client, engine = make_client(ai=FakeAI())
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
        deps = client.app.state.deps
        links = start_case(deps.context(session), "MA")
    return client, links


def photo():
    jpeg = render_bill(make_truth(random.Random(1)), random.Random(1), layout=0, rotate_deg=0.0, blur=0.0)
    return {"photo": ("bill.jpg", jpeg, "image/jpeg")}


def test_senior_flow_from_photo_to_result():
    client, links = seeded_client()
    base = f"/s/{links.senior_token}"
    start = client.get(base)
    assert start.status_code == 200
    assert 'capture="environment"' in start.text and "Take a photo" in start.text

    readback = client.post(f"{base}/bill", files=photo())
    assert readback.status_code == 200
    assert "ST. EXAMPLE MEDICAL CENTER" in readback.text
    assert "$1,850.00" in readback.text and "September 3, 2026" in readback.text
    assert re.search(r"Yes, that.?s right", readback.text)

    household = client.post(f"{base}/confirm", data={"answer": "yes"}, follow_redirects=True)
    assert "How many people" in household.text

    income = client.post(f"{base}/household", data={"size": "1", "programs": "none"}, follow_redirects=True)
    assert "Social Security" in income.text

    result = client.post(f"{base}/income", files=photo(), follow_redirects=True)
    assert result.status_code == 200
    assert "likely do not have to pay" in result.text
    assert "Read this to me" in result.text
    assert "1850" not in str(result.url)


def test_senior_says_not_right_and_waits_for_helper():
    client, links = seeded_client()
    base = f"/s/{links.senior_token}"
    client.post(f"{base}/bill", files=photo())
    wait = client.post(f"{base}/confirm", data={"answer": "no"}, follow_redirects=True)
    assert "helper" in wait.text.lower()


def test_tampered_token_is_rejected():
    client, links = seeded_client()
    assert client.get("/s/" + links.senior_token[:-3] + "abc").status_code == 403
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_web_senior.py -v`
Expected: FAIL (the placeholder returns JSON, so the "Take a photo" assertion fails)

- [ ] **Step 3: Implement the routes**

`src/waive/web/routes_senior.py`:

```python
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
    }


def money(value: Decimal | None) -> str:
    return "" if value is None else f"${value:,.2f}"


def long_date(value) -> str:
    return "" if value is None else f"{value:%B} {value.day}, {value:%Y}"


def _page(request: Request, name: str, token: str, shown, **extra) -> HTMLResponse:
    return render(
        request, name, links=senior_links(token), case=shown, money=money, long_date=long_date, **extra
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
async def senior_bill(request: Request, token: str, photo: UploadFile = File(...)) -> HTMLResponse:
    deps = deps_of(request)
    data = await photo.read()
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        if deps.ai is None:
            return _page(
                request, "senior_wait.html", token, view(ctx, row.id), reason="reading is switched off right now"
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
            request, "senior_wait.html", token, view(ctx, row.id), reason="your helper will fix the details"
        )


@router.get("/s/{token}/household", response_class=HTMLResponse)
def senior_household_form(request: Request, token: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "senior")
        return _page(request, "senior_household.html", token, view(ctx, row.id))


@router.post("/s/{token}/household")
def senior_household(request: Request, token: str, size: int = Form(...), programs: str = Form("none")):
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
    photo: UploadFile | None = File(None),
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
            return _page(request, "senior_wait.html", token, shown, reason="your helper will finish the check")
        return _page(request, "senior_result.html", token, shown)
```

- [ ] **Step 4: Write the templates**

`senior_start.html`:

```html
{% extends "base.html" %}
{% block title %}Show us the bill{% endblock %}
{% block content %}
<h1>Let's look at your hospital bill</h1>
{% if problem %}<p class="card big">{{ problem }}</p>{% endif %}
<p class="big">Lay the bill flat. Take a photo of the whole page.</p>
<form method="post" action="{{ links.bill }}" enctype="multipart/form-data" class="stack">
  <label for="photo" class="visually-hidden">Photo of the bill</label>
  <input id="photo" type="file" name="photo" accept="image/*" capture="environment" required onchange="this.form.submit()">
  <button type="submit" class="btn primary">Take a photo of the bill</button>
</form>
<p class="muted">Your bill stays private. We only use it to check the hospital's rules.</p>
{% endblock %}
```

`senior_readback.html`:

```html
{% extends "base.html" %}
{% block title %}Is this right?{% endblock %}
{% block content %}
<h1>Here is what we read</h1>
<div class="card big">
  <p>Hospital: <strong>{{ case.hospital_name or case.bill.hospital_name or "we could not read it" }}</strong></p>
  <p>Amount due: <strong>{{ money(case.bill.amount_due) or "we could not read it" }}</strong></p>
  <p>Statement date: <strong>{{ long_date(case.bill.statement_date) or "we could not read it" }}</strong></p>
</div>
<form method="post" action="{{ links.confirm }}" class="choices">
  <button type="submit" name="answer" value="yes" class="btn primary">Yes, that's right</button>
  <button type="submit" name="answer" value="no" class="btn">Something is wrong</button>
</form>
{% endblock %}
```

`senior_household.html`:

```html
{% extends "base.html" %}
{% block title %}Two quick questions{% endblock %}
{% block content %}
<h1>How many people live in your home, counting you?</h1>
<form method="post" action="{{ links.household }}" class="stack">
  <div class="choices">
    {% for n in [1, 2, 3, 4] %}
    <label class="btn"><input type="radio" name="size" value="{{ n }}" {% if n == 1 %}checked{% endif %}> {{ n }}{% if n == 4 %} or more{% endif %}</label>
    {% endfor %}
  </div>
  <h2>Do you have MassHealth or SNAP (food help)?</h2>
  <div class="choices">
    <label class="btn"><input type="radio" name="programs" value="MassHealth"> Yes, MassHealth</label>
    <label class="btn"><input type="radio" name="programs" value="SNAP"> Yes, SNAP</label>
    <label class="btn"><input type="radio" name="programs" value="MassHealth,SNAP"> Both</label>
    <label class="btn"><input type="radio" name="programs" value="none" checked> No, or not sure</label>
  </div>
  <button type="submit" class="btn primary">Next</button>
</form>
{% endblock %}
```

`senior_income.html`:

```html
{% extends "base.html" %}
{% block title %}Your income{% endblock %}
{% block content %}
<h1>Do you have your Social Security letter?</h1>
{% if problem %}<p class="card big">{{ problem }}</p>{% endif %}
<p class="big">It says how much you get each month. A photo of it is all we need.</p>
<form method="post" action="{{ links.income }}" enctype="multipart/form-data" class="stack">
  <label for="photo" class="visually-hidden">Photo of the letter</label>
  <input id="photo" type="file" name="photo" accept="image/*" capture="environment" onchange="this.form.submit()">
  <button type="submit" class="btn primary">Take a photo of the letter</button>
</form>
<form method="post" action="{{ links.income }}" class="stack">
  <button type="submit" name="skip" value="1" class="btn quiet">I don't have it. My helper will fill it in.</button>
</form>
{% endblock %}
```

`senior_result.html`:

```html
{% extends "base.html" %}
{% block title %}What we found{% endblock %}
{% block content %}
<h1>What we found</h1>
<div class="card big status-{{ case.tier.value }}" id="result-text">{{ case.senior_text }}</div>
<button type="button" class="btn" id="speak" hidden>Read this to me</button>
<h2>What happens next</h2>
<p class="big">Your helper will check this. Then we print the form for you to sign and mail. You never pay anything to use Waive.</p>
{% endblock %}
{% block scripts %}
<script>
(function () {
  var button = document.getElementById("speak");
  if (!("speechSynthesis" in window)) { return; }
  button.hidden = false;
  button.addEventListener("click", function () {
    var text = document.getElementById("result-text").textContent;
    var utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 0.9;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  });
})();
</script>
{% endblock %}
```

Note: "Read this to me" must appear in the HTML even though the button starts hidden (the test checks the text; the browser shows it when speech is available).

`senior_wait.html`:

```html
{% extends "base.html" %}
{% block title %}Thank you{% endblock %}
{% block content %}
<h1>Thank you</h1>
<p class="big">You are done for now: {{ reason }}. Your helper will take it from here.</p>
<p><a href="{{ links.start }}" class="btn quiet">Start again with a new photo</a></p>
{% endblock %}
```

- [ ] **Step 5: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_web_senior.py -v`
Expected: 3 passed

- [ ] **Step 6: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add src/waive/web tests/unit/test_web_senior.py
git commit -m "feat: senior phone flow from photo to spoken result"
```

---

### Task 4.3: Caregiver flow

**Files:**
- Replace: `src/waive/web/routes_caregiver.py`; create templates `case_created.html`, `caregiver_review.html` (`home.html` already renders `notice`)
- Test: `tests/unit/test_web_caregiver.py`

**Interfaces:**
- Routes: `POST /cases` (`state`) → `case_created.html` with both links · `GET /c/{token}` review · `POST /c/{token}/correct` (`hospital_ccn`, `amount_due`, `statement_date`, `size`, `annual_income`, `programs`) · `POST /c/{token}/approve` · `POST /c/{token}/delete` → home with a notice.
- Helper: `caregiver_links(token) -> dict` (`review, correct, approve, delete, packet, reminders`).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_web_caregiver.py`:

```python
import re

from tests.unit.test_web_senior import photo, seeded_client


def test_create_case_shows_both_links_and_review_works():
    client, _ = seeded_client()
    created = client.post("/cases", data={"state": "MA"})
    assert created.status_code == 200
    senior = re.search(r'href="(/s/[^"]+)"', created.text).group(1)
    caregiver = re.search(r'href="(/c/[^"]+)"', created.text).group(1)
    assert "Share this link" in created.text

    client.post(f"{senior}/bill", files=photo())
    review = client.get(caregiver)
    assert review.status_code == 200
    assert "ST. EXAMPLE MEDICAL CENTER" in review.text and "$1,850.00" in review.text

    corrected = client.post(
        f"{caregiver}/correct",
        data={
            "hospital_ccn": "229999",
            "amount_due": "1850.00",
            "statement_date": "2026-09-03",
            "size": "1",
            "annual_income": "22800",
            "programs": "",
        },
        follow_redirects=True,
    )
    assert "Likely free care" in corrected.text
    assert "Policy says" in corrected.text and "250%" in corrected.text
    assert "May 1, 2027" in corrected.text

    approved = client.post(f"{caregiver}/approve", follow_redirects=True)
    assert "Approved" in approved.text and "Download the packet" in approved.text

    senior_token = senior.rsplit("/", 1)[1]
    assert client.post(f"/c/{senior_token}/approve").status_code == 403

    gone = client.post(f"{caregiver}/delete", follow_redirects=True)
    assert "deleted" in gone.text.lower()
    assert client.get(caregiver).status_code in (403, 404)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/unit/test_web_caregiver.py -v`
Expected: FAIL (405 or 404 on `/cases`)

- [ ] **Step 3: Implement**

`src/waive/web/routes_caregiver.py`:

```python
"""Caregiver flow: create a case, review, correct, approve, delete (spec §9 steps 3 and 9)."""

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from waive.atlas import repo
from waive.cases.service import (
    approve,
    authorize,
    confirm_bill,
    delete_case,
    set_household,
    start_case,
    view,
)
from waive.db import session_scope
from waive.web.deps import deps_of, render
from waive.web.routes_senior import long_date, money, senior_links

router = APIRouter()


def caregiver_links(token: str) -> dict[str, str]:
    base = f"/c/{token}"
    return {
        "review": base,
        "correct": f"{base}/correct",
        "approve": f"{base}/approve",
        "delete": f"{base}/delete",
        "packet": f"{base}/packet.pdf",
        "reminders": f"{base}/reminders.ics",
    }


@router.post("/cases", response_class=HTMLResponse)
def create_case(request: Request, state: str = Form("MA")) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        links = start_case(deps.context(session), state)
    return render(
        request,
        "case_created.html",
        senior_url=senior_links(links.senior_token)["start"],
        caregiver_url=caregiver_links(links.caregiver_token)["review"],
    )


@router.get("/c/{token}", response_class=HTMLResponse)
def caregiver_review(request: Request, token: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        shown = view(ctx, row.id)
        found = repo.latest_sheet(session, row.ccn) if row.ccn else None
        sheet = found[0] if found else None
        sources = {source.id: source for source in sheet.sources} if sheet else {}
        return render(
            request,
            "caregiver_review.html",
            links=caregiver_links(token),
            case=shown,
            sheet=sheet,
            sources=sources,
            money=money,
            long_date=long_date,
        )


def _decimal(value: str) -> Decimal | None:
    text = value.replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


@router.post("/c/{token}/correct")
def caregiver_correct(
    request: Request,
    token: str,
    hospital_ccn: str = Form(""),
    amount_due: str = Form(""),
    statement_date: str = Form(""),
    size: int = Form(1),
    annual_income: str = Form(""),
    programs: str = Form(""),
):
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        corrections: dict = {}
        amount = _decimal(amount_due)
        if amount is not None:
            corrections["amount_due"] = str(amount)
        if statement_date:
            corrections["statement_date"] = date.fromisoformat(statement_date).isoformat()
        confirm_bill(ctx, row.id, corrections, ccn=hospital_ccn or None)
        chosen = tuple(p.strip() for p in programs.split(",") if p.strip())
        set_household(ctx, row.id, size, _decimal(annual_income), chosen)
    return RedirectResponse(caregiver_links(token)["review"], status_code=303)


@router.post("/c/{token}/approve")
def caregiver_approve(request: Request, token: str):
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        approve(ctx, row.id)
    return RedirectResponse(caregiver_links(token)["review"], status_code=303)


@router.post("/c/{token}/delete", response_class=HTMLResponse)
def caregiver_delete(request: Request, token: str) -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        delete_case(ctx, row.id)
    return render(request, "home.html", notice="The case and all its personal data were deleted.")
```

`case_created.html`:

```html
{% extends "base.html" %}
{% block title %}Case created{% endblock %}
{% block content %}
<h1>Two links</h1>
<h2>1. For the person with the bill</h2>
<p class="big">Share this link with them. It only lets them add photos and see the result.</p>
<p><a class="btn primary" href="{{ senior_url }}" id="senior-link">Open the bill-photo page</a></p>
<button type="button" class="btn" id="share" hidden>Share this link</button>
<h2>2. For you</h2>
<p class="big">Keep this link. It lets you review, fix, approve and delete.</p>
<p><a class="btn" href="{{ caregiver_url }}">Open your review page</a></p>
{% endblock %}
{% block scripts %}
<script>
(function () {
  var button = document.getElementById("share");
  var url = new URL(document.getElementById("senior-link").getAttribute("href"), window.location.href).href;
  button.hidden = false;
  button.addEventListener("click", function () {
    if (navigator.share) { navigator.share({ title: "Waive", url: url }); }
    else if (navigator.clipboard) { navigator.clipboard.writeText(url).then(function () { button.textContent = "Link copied"; }); }
  });
})();
</script>
{% endblock %}
```

`caregiver_review.html`:

```html
{% extends "base.html" %}
{% block title %}Review{% endblock %}
{% block content %}
<h1>Review</h1>
{% if case.status == "approved" %}<p class="card status-free big">Approved. Print the packet, have it signed, and send it.</p>{% endif %}
{% if case.status == "new" %}<p class="big">Waiting for a photo of the bill.</p>{% endif %}

{% if case.bill %}
<h2>What we read from the bill</h2>
<div class="card">
  <div class="row"><span>Hospital</span><strong>{{ case.hospital_name or case.bill.hospital_name or "unknown" }}</strong></div>
  <div class="row"><span>Amount due</span><strong>{{ money(case.bill.amount_due) }}</strong></div>
  <div class="row"><span>Statement date</span><strong>{{ long_date(case.bill.statement_date) }}</strong></div>
  {% if case.warnings %}<div class="row"><span>Photo</span><span>{{ case.warnings | join(", ") }}</span></div>{% endif %}
</div>
{% if case.needs_scouting %}<p class="card status-unknown">We do not have this hospital's policy yet. Our scouts have been asked to fetch it. Pick the hospital below if you recognise it.</p>{% endif %}
{% endif %}

{% if case.result %}
<h2>Result</h2>
<div class="card status-{{ case.tier.value }}">
  {% for line in case.caregiver_text.split("\n") %}
    {% if line.startswith("Policy says") %}<p class="quote">{{ line }}</p>{% else %}<p>{{ line }}</p>{% endif %}
  {% endfor %}
  {% for reason in case.result.reasons %}{% if reason.source_id and reason.source_id in sources %}
    <p class="muted">Source: <a href="{{ sources[reason.source_id].url }}">{{ sources[reason.source_id].title or "policy document" }}</a> (checked {{ sources[reason.source_id].fetched_on }})</p>
  {% endif %}{% endfor %}
</div>
{% endif %}

{% if case.deadlines %}
<h2>Dates that matter</h2>
<div class="card">
  <div class="row"><span>Apply by</span><strong>{{ long_date(case.deadlines.application_deadline) }}</strong></div>
  <div class="row"><span>No collections before</span><strong>{{ long_date(case.deadlines.collections_allowed_from) }}</strong></div>
  {% if case.deadlines.collection_notice_too_early %}<p>The collection notice came too early. The packet includes a note about this.</p>{% endif %}
</div>
{% endif %}

<h2>Fix or add details</h2>
<form method="post" action="{{ links.correct }}" class="stack">
  <label for="hospital_ccn">Hospital</label>
  <select id="hospital_ccn" name="hospital_ccn">
    <option value="">Keep: {{ case.hospital_name or "unknown" }}</option>
    {% for candidate in case.candidates %}<option value="{{ candidate.ccn }}">{{ candidate.name }} (match {{ candidate.score }})</option>{% endfor %}
  </select>
  <label for="amount_due">Amount due</label>
  <input id="amount_due" name="amount_due" inputmode="decimal" value="{{ case.bill.amount_due if case.bill and case.bill.amount_due else '' }}">
  <label for="statement_date">Statement date</label>
  <input id="statement_date" name="statement_date" type="date" value="{{ case.bill.statement_date if case.bill and case.bill.statement_date else '' }}">
  <label for="size">People in the household</label>
  <input id="size" name="size" type="number" min="1" max="12" value="{{ case.household_size or 1 }}">
  <label for="annual_income">Yearly household income (dollars)</label>
  <input id="annual_income" name="annual_income" inputmode="decimal" value="{{ case.annual_income or '' }}">
  <label for="programs">Programs (for example MassHealth, SNAP)</label>
  <input id="programs" name="programs" value="">
  <button type="submit" class="btn">Save and re-check</button>
</form>

{% if case.result and case.status != "approved" %}
<form method="post" action="{{ links.approve }}" class="stack"><button type="submit" class="btn primary">Approve: prepare the packet</button></form>
{% endif %}
{% if case.status == "approved" %}
<p><a class="btn primary" href="{{ links.packet }}">Download the packet (PDF)</a></p>
<p><a class="btn" href="{{ links.reminders }}">Add reminders to my calendar</a></p>
{% endif %}
<form method="post" action="{{ links.delete }}" class="stack" onsubmit="return confirm('Delete this case and all its personal data?')">
  <button type="submit" class="btn danger">Delete this case</button>
</form>
{% endblock %}
```

- [ ] **Step 4: Run the test to make sure it passes**

Run: `uv run pytest tests/unit/test_web_caregiver.py -v`
Expected: 1 passed

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add src/waive/web tests/unit/test_web_caregiver.py
git commit -m "feat: caregiver review, correction, approval and delete flow"
```

---

### Task 4.4: Application packet (PDF)

**Files:**
- Create: `src/waive/cases/packet.py`
- Modify: `src/waive/web/routes_caregiver.py` (add `GET /c/{token}/packet.pdf`), `pyproject.toml` (add `reportlab>=4.2`), `tests/unit/test_web_caregiver.py` (packet assertion)
- Test: `tests/unit/test_packet.py`

**Interfaces:**
- Produces: `PacketData(hospital_name, submit_lines: list[str], form_url: str | None, patient_name, account_reference, amount_due, statement_date, household_size, annual_income, tier_text, reasons: list[tuple[str, str]], documents: list[str], deadlines: Deadlines | None, collection_too_early: bool, today: date)`; `packet_data(shown: CaseView, sheet: ProcedureSheet, today) -> PacketData`; `build_packet(data: PacketData) -> bytes` (PDF).

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_packet.py`:

```python
from datetime import date
from decimal import Decimal

from waive.atlas.samples import st_example_sheet
from waive.cases.extract import BillExtract
from waive.cases.packet import PacketData, build_packet, packet_data
from waive.cases.service import CaseView
from waive.rules.deadlines import compute_deadlines
from waive.rules.eligibility import Household, evaluate_eligibility

TODAY = date(2026, 10, 2)


def test_packet_data_pulls_submit_methods_and_documents():
    sheet = st_example_sheet()
    result = evaluate_eligibility(sheet, Household(1, Decimal("22800"), "MA"))
    shown = CaseView(
        case_id="c",
        status="approved",
        hospital_name="St. Example Medical Center",
        ccn="229999",
        bill=BillExtract(
            patient_name="Rosa Alvarez",
            account_reference="ACCT-1",
            amount_due=Decimal("1850"),
            statement_date=date(2026, 9, 3),
        ),
        household_size=1,
        annual_income=Decimal("22800"),
        tier=result.tier,
        result=result,
        deadlines=compute_deadlines(date(2026, 9, 3), TODAY),
    )
    data = packet_data(shown, sheet, TODAY)
    assert data.submit_lines == [
        "Mail: Patient Financial Services, 1 Example Way, Boston, MA 02118",
        "Fax: 617-555-0199",
    ]
    assert data.documents == ["Photo ID", "Proof of income"]
    assert data.reasons[0][1].startswith("household income at or below 250%")
    assert data.tier_text.startswith("Likely free care")


def test_build_packet_returns_pdf():
    data = PacketData(
        hospital_name="St. Example Medical Center",
        submit_lines=["Fax: 617-555-0199"],
        form_url=None,
        patient_name="Rosa Alvarez",
        account_reference="ACCT-1",
        amount_due=Decimal("1850"),
        statement_date=date(2026, 9, 3),
        household_size=1,
        annual_income=Decimal("22800"),
        tier_text="Likely free care",
        reasons=[("Income is 143% of the poverty line", "household income at or below 250%")],
        documents=["Photo ID"],
        deadlines=compute_deadlines(date(2026, 9, 3), TODAY),
        collection_too_early=True,
        today=TODAY,
    )
    pdf = build_packet(data)
    assert pdf[:5] == b"%PDF-" and len(pdf) > 2000
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv add "reportlab>=4.2" && uv run pytest tests/unit/test_packet.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'waive.cases.packet'`

- [ ] **Step 3: Implement**

`src/waive/cases/packet.py`:

```python
"""The printable application packet: cover letter, data sheet, checklist (spec §9 step 8)."""

import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from waive.atlas.schema import ProcedureSheet
from waive.cases.service import CaseView
from waive.rules.deadlines import Deadlines
from waive.rules.explain import caregiver_summary

DOC_LABELS = {
    "photo_id": "Photo ID",
    "proof_of_income": "Proof of income",
    "social_security_letter": "Social Security benefit letter",
    "tax_return": "Most recent tax return",
    "pay_stubs": "Recent pay stubs",
    "bank_statements": "Bank statements",
    "proof_of_residency": "Proof of address",
    "insurance_card": "Insurance card",
    "medicaid_denial": "Medicaid (MassHealth) denial letter",
    "other": "Other documents the hospital asks for",
}
METHOD_LABELS = {"mail": "Mail", "fax": "Fax", "email": "Email", "portal": "Online", "in_person": "In person"}


@dataclass
class PacketData:
    hospital_name: str
    submit_lines: list[str]
    form_url: str | None
    patient_name: str | None
    account_reference: str | None
    amount_due: Decimal | None
    statement_date: date | None
    household_size: int | None
    annual_income: Decimal | None
    tier_text: str
    reasons: list[tuple[str, str]]
    documents: list[str]
    deadlines: Deadlines | None
    collection_too_early: bool
    today: date


def packet_data(shown: CaseView, sheet: ProcedureSheet, today: date) -> PacketData:
    methods = sheet.apply.submit_methods.value if sheet.apply.submit_methods else []
    documents = sheet.apply.documents_required.value if sheet.apply.documents_required else []
    result = shown.result
    bill = shown.bill
    name = shown.hospital_name or sheet.hospital.name
    return PacketData(
        hospital_name=name,
        submit_lines=[f"{METHOD_LABELS[m.kind]}: {m.detail}" for m in methods],
        form_url=sheet.apply.form_url.value if sheet.apply.form_url else None,
        patient_name=bill.patient_name if bill else None,
        account_reference=bill.account_reference if bill else None,
        amount_due=bill.amount_due if bill else None,
        statement_date=bill.statement_date if bill else None,
        household_size=shown.household_size,
        annual_income=shown.annual_income,
        tier_text=caregiver_summary(result, name).split("\n")[0] if result else "",
        reasons=[(r.text, r.quote or "") for r in (result.reasons if result else [])],
        documents=[DOC_LABELS.get(d.value, d.value) for d in documents],
        deadlines=shown.deadlines,
        collection_too_early=bool(shown.deadlines and shown.deadlines.collection_notice_too_early),
        today=today,
    )


def build_packet(data: PacketData) -> bytes:
    styles = getSampleStyleSheet()
    body, title, heading = styles["BodyText"], styles["Title"], styles["Heading2"]
    body.fontSize, body.leading = 11, 15
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=letter, leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch
    )
    flow = [Paragraph("Request for financial assistance", title), Spacer(1, 12)]
    flow.append(Paragraph(f"Date: {data.today:%B %d, %Y}", body))
    flow.append(Paragraph(f"To: Patient Financial Services, {data.hospital_name}", body))
    for line in data.submit_lines:
        flow.append(Paragraph(line, body))
    flow.append(Spacer(1, 12))
    if data.statement_date:
        opening = (
            "I am writing to apply for financial assistance under your Financial Assistance Policy for the "
            f"statement dated {data.statement_date:%B %d, %Y} (account "
            f"{data.account_reference or 'see enclosed statement'}), amount due ${data.amount_due or 0:,.2f}."
        )
    else:
        opening = (
            "I am writing to apply for financial assistance under your Financial Assistance Policy for the "
            "enclosed statement."
        )
    flow.append(Paragraph(opening, body))
    flow.append(Spacer(1, 8))
    flow.append(Paragraph(f"Based on your published policy, I believe I qualify. {data.tier_text}", body))
    for text, quote in data.reasons:
        flow.append(Paragraph(text, body))
        if quote:
            flow.append(Paragraph(f'Your policy states: "{quote}"', body))
    if data.collection_too_early:
        flow.append(Spacer(1, 8))
        flow.append(
            Paragraph(
                "Note: I received a collection notice fewer than 120 days after the first statement. Under "
                "26 CFR 1.501(r)-6, please pause any collection activity while this application is pending.",
                body,
            )
        )
    flow.append(Spacer(1, 8))
    flow.append(Paragraph("Please contact me if you need anything else. Thank you.", body))
    flow.append(Spacer(1, 36))
    flow.append(Paragraph("Signature: ______________________________   Date: ______________", body))
    flow.append(Paragraph(f"Name: {data.patient_name or '______________________________'}", body))
    flow.append(PageBreak())
    flow.append(Paragraph("Application information", heading))
    rows = [
        ("Patient name", data.patient_name or ""),
        ("Account number", data.account_reference or ""),
        ("Statement date", f"{data.statement_date:%m/%d/%Y}" if data.statement_date else ""),
        ("Amount due", f"${data.amount_due:,.2f}" if data.amount_due is not None else ""),
        ("People in household", str(data.household_size or "")),
        ("Yearly household income", f"${data.annual_income:,.0f}" if data.annual_income is not None else ""),
    ]
    for label, value in rows:
        flow.append(Paragraph(f"<b>{label}:</b> {value}", body))
    if data.form_url:
        flow.append(Spacer(1, 8))
        flow.append(Paragraph(f"The hospital's own application form: {data.form_url}", body))
    flow.append(Paragraph("Documents to enclose", heading))
    for item in data.documents or ["Photo ID", "Proof of income"]:
        flow.append(Paragraph(f"[ ] {item}", body))
    flow.append(Paragraph("[ ] A copy of the hospital statement", body))
    if data.deadlines:
        flow.append(Paragraph("Dates", heading))
        flow.append(Paragraph(f"Apply by: {data.deadlines.application_deadline:%B %d, %Y}", body))
        flow.append(
            Paragraph(
                f"No collection actions allowed before: {data.deadlines.collections_allowed_from:%B %d, %Y}", body
            )
        )
    flow.append(Spacer(1, 12))
    flow.append(
        Paragraph(
            "Prepared with Waive. This is an estimate based on the hospital's published policy; the hospital "
            "makes the final decision. Waive never asks for payment.",
            body,
        )
    )
    document.build(flow)
    return buffer.getvalue()
```

Add to `src/waive/web/routes_caregiver.py`:

```python
from fastapi.responses import Response

from waive.cases.packet import build_packet, packet_data


@router.get("/c/{token}/packet.pdf")
def caregiver_packet(request: Request, token: str) -> Response:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        shown = view(ctx, row.id)
        found = repo.latest_sheet(session, row.ccn) if row.ccn else None
        if found is None or shown.result is None:
            raise KeyError("packet not ready")
        pdf = build_packet(packet_data(shown, found[0], ctx.today))
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="waive-application.pdf"'},
    )
```

Add to the end of the flow test in `tests/unit/test_web_caregiver.py`, right after the approval assertions:

```python
    pdf = client.get(f"{caregiver}/packet.pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
```

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `uv run pytest tests/unit/test_packet.py tests/unit/test_web_caregiver.py -v`
Expected: 3 passed

- [ ] **Step 5: Lint, format, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add pyproject.toml uv.lock src/waive/cases/packet.py src/waive/web/routes_caregiver.py tests/unit/test_packet.py tests/unit/test_web_caregiver.py
git commit -m "feat: printable application packet PDF with cited policy quotes"
```

---

### Task 4.5: Calendar reminders (`.ics`)

**Files:**
- Create: `src/waive/cases/reminders.py`
- Modify: `src/waive/web/routes_caregiver.py` (add `GET /c/{token}/reminders.ics`)
- Test: `tests/unit/test_reminders.py`

**Interfaces:**
- Produces: `reminder_events(sent_on: date, deadlines: Deadlines | None, hospital_name: str) -> list[tuple[date, str, str]]` (date, summary, description) for sent+14, +30, +45, collections−7, deadline−14; `build_ics(events) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_reminders.py`:

```python
from datetime import date

from waive.cases.reminders import build_ics, reminder_events
from waive.rules.deadlines import compute_deadlines


def test_events_cover_checkins_and_deadlines():
    deadlines = compute_deadlines(date(2026, 9, 3), date(2026, 10, 2))
    events = reminder_events(date(2026, 10, 2), deadlines, "St. Example")
    dates = [d for d, _, _ in events]
    assert dates == [
        date(2026, 10, 16),
        date(2026, 11, 1),
        date(2026, 11, 16),
        date(2026, 12, 25),
        date(2027, 4, 17),
    ]
    assert "St. Example" in events[0][1]


def test_ics_is_well_formed():
    ics = build_ics([(date(2026, 10, 16), "Check on the application", "Call the hospital")])
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.rstrip().endswith("END:VCALENDAR")
    assert "DTSTART;VALUE=DATE:20261016" in ics and "SUMMARY:Check on the application" in ics
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `uv run pytest tests/unit/test_reminders.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement**

`src/waive/cases/reminders.py`:

```python
"""Calendar reminders for check-ins and 501(r) deadlines (spec §9 step 10)."""

import uuid
from datetime import date, timedelta

from waive.rules.deadlines import Deadlines


def reminder_events(
    sent_on: date, deadlines: Deadlines | None, hospital_name: str
) -> list[tuple[date, str, str]]:
    events = [
        (
            sent_on + timedelta(days=14),
            f"Check on the {hospital_name} application",
            "Call the hospital's financial assistance office and ask whether they need anything else.",
        ),
        (
            sent_on + timedelta(days=30),
            f"Follow up with {hospital_name}",
            "Ask for the decision or an update in writing.",
        ),
        (
            sent_on + timedelta(days=45),
            f"Still waiting on {hospital_name}?",
            "If there is no decision yet, ask for the expected date and note who you spoke to.",
        ),
    ]
    if deadlines:
        events.append(
            (
                deadlines.collections_allowed_from - timedelta(days=7),
                f"Collections protection ends soon: {hospital_name}",
                "If the application is not decided, send a written reminder that it is pending.",
            )
        )
        events.append(
            (
                deadlines.application_deadline - timedelta(days=14),
                f"Last chance to apply: {hospital_name}",
                "The 240-day application window closes in two weeks.",
            )
        )
    return events


def build_ics(events: list[tuple[date, str, str]]) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Waive//EN"]
    for day, summary, description in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uuid.uuid4()}@waive",
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
```

Add to `routes_caregiver.py`:

```python
from waive.cases.reminders import build_ics, reminder_events


@router.get("/c/{token}/reminders.ics")
def caregiver_reminders(request: Request, token: str) -> Response:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        ctx = deps.context(session)
        row = authorize(ctx, token, "caregiver")
        shown = view(ctx, row.id)
        ics = build_ics(reminder_events(ctx.today, shown.deadlines, shown.hospital_name or "the hospital"))
    return Response(
        ics,
        media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="waive-reminders.ics"'},
    )
```

- [ ] **Step 4: Run the tests, then lint, format, commit**

Run: `uv run pytest tests/unit/test_reminders.py -v` → 2 passed.

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add src/waive/cases/reminders.py src/waive/web/routes_caregiver.py tests/unit/test_reminders.py
git commit -m "feat: calendar reminders for check-ins and deadlines"
```

---

### Task 4.6: Public atlas pages

**Files:**
- Replace: `src/waive/web/routes_atlas.py`; create templates `atlas_list.html`, `atlas_sheet.html`
- Test: `tests/unit/test_web_atlas.py`

**Interfaces:**
- Routes: `GET /atlas?q=` list (name, city, status, version) · `GET /atlas/{ccn}` sheet with every field (value, layer, quote, source link, checked date), sources and version · `GET /atlas/{ccn}.json` latest sheet JSON.

- [ ] **Step 1: Write the failing test**

`tests/unit/test_web_atlas.py`:

```python
from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.db import session_scope

from tests.unit.test_web_app import make_client
from tests.unit.test_web_senior import HOSPITAL


def test_atlas_pages_and_json():
    client, engine = make_client()
    with session_scope(engine) as session:
        repo.upsert_hospital(session, HOSPITAL)
        publish_sheet(session, st_example_sheet())
    listing = client.get("/atlas?q=example")
    assert listing.status_code == 200
    assert "ST. EXAMPLE MEDICAL CENTER" in listing.text and "published" in listing.text
    page = client.get("/atlas/229999")
    assert "free_care_max_fpl" in page.text and "250" in page.text
    assert "household income at or below 250%" in page.text
    assert "example.org" in page.text
    data = client.get("/atlas/229999.json").json()
    assert data["version"] == 1 and data["eligibility"]["free_care_max_fpl"]["value"] == "250"
    assert client.get("/atlas/000000").status_code == 404
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `uv run pytest tests/unit/test_web_atlas.py -v` → FAIL (404)

- [ ] **Step 3: Implement**

`src/waive/web/routes_atlas.py`:

```python
"""Public, read-only atlas pages (spec §7: open, cited, versioned)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from waive.atlas import repo
from waive.db import session_scope
from waive.web.deps import deps_of, render

router = APIRouter()


@router.get("/atlas", response_class=HTMLResponse)
def atlas_list(request: Request, q: str = "") -> HTMLResponse:
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        rows = []
        for hospital in repo.list_hospitals(session):
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
        fields = [(path, cited, sources.get(cited.source_id)) for path, cited in sheet.field_paths()]
        return render(request, "atlas_sheet.html", sheet=sheet, fields=fields, created=row.created_at)
```

`atlas_list.html`:

```html
{% extends "base.html" %}
{% block title %}Hospital policies{% endblock %}
{% block content %}
<h1>Hospital financial assistance policies</h1>
<form method="get" action="/atlas" class="stack">
  <label for="q">Search by hospital or city</label>
  <input id="q" name="q" value="{{ q }}">
  <button class="btn" type="submit">Search</button>
</form>
{% for row in rows %}
<div class="card"><a href="/atlas/{{ row.ccn }}"><strong>{{ row.name }}</strong></a><br>{{ row.city }}, {{ row.state }} — {{ row.status }}{% if row.version %} (version {{ row.version }}){% endif %}</div>
{% else %}<p>No hospitals match.</p>{% endfor %}
<p class="muted">Open data, CC BY 4.0. Every documented value shows the exact sentence it came from.</p>
{% endblock %}
```

`atlas_sheet.html`:

```html
{% extends "base.html" %}
{% block title %}{{ sheet.hospital.name }}{% endblock %}
{% block content %}
<h1>{{ sheet.hospital.name }}</h1>
<p class="muted">{{ sheet.hospital.city }}, {{ sheet.hospital.state }} · version {{ sheet.version }} · {{ sheet.status.value }} · <a href="/atlas/{{ sheet.hospital.ccn }}.json">JSON</a></p>
{% for path, cited, source in fields %}
<div class="card">
  <div class="row"><span><code>{{ path }}</code></span><strong>{{ cited.value }}</strong></div>
  {% if cited.quote %}<p class="quote">"{{ cited.quote }}"</p>{% endif %}
  <p class="muted">{{ cited.layer.value }} · checked {{ cited.checked_on }}{% if source %} · <a href="{{ source.url }}">{{ source.title or source.url }}</a>{% endif %}{% if cited.support_count %} · reported by {{ cited.support_count }} people{% endif %}</p>
</div>
{% endfor %}
<h2>Sources</h2>
{% for source in sheet.sources %}<p><a href="{{ source.url }}">{{ source.title or source.url }}</a> — fetched {{ source.fetched_on }}</p>{% endfor %}
{% endblock %}
```

- [ ] **Step 4: Run the test, then lint, format, commit**

Run: `uv run pytest tests/unit/test_web_atlas.py -v` → 1 passed.

```bash
uv run ruff format . && uv run ruff check . && uv run pytest
git add src/waive/web tests/unit/test_web_atlas.py
git commit -m "feat: public atlas pages with quotes, sources and JSON"
```

---

### Task 4.7: Demo seed and phone test (gate U4.1)

**Files:**
- Create: `src/waive/demo.py`
- Modify: `src/waive/cli.py` (add `waive demo seed` and `waive demo forget-cases`)
- Test: `tests/unit/test_demo.py`

**Interfaces:**
- Produces: `seed_demo(session) -> None` (upserts the fictional St. Example hospital and publishes `st_example_sheet()` if absent); `forget_cases(session) -> int` (deletes every case row and returns the count).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_demo.py`:

```python
from waive.atlas import repo
from waive.db import CaseRow, init_db, make_engine, session_scope
from waive.demo import forget_cases, seed_demo


def test_seed_and_forget():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        seed_demo(session)
        seed_demo(session)
        assert repo.latest_sheet(session, "229999")[0].version == 1
        session.add(CaseRow(id="x", state="MA", status="new", token_generation=1))
    with session_scope(engine) as session:
        assert forget_cases(session) == 1
```

- [ ] **Step 2: Implement**

`src/waive/demo.py`:

```python
"""Fictional demo data so the app can be tried without keys or real hospitals."""

from sqlalchemy import delete
from sqlalchemy.orm import Session

from waive.atlas import repo
from waive.atlas.publish import publish_sheet
from waive.atlas.samples import st_example_sheet
from waive.db import CaseRow


def seed_demo(session: Session) -> None:
    sheet = st_example_sheet()
    h = sheet.hospital
    repo.upsert_hospital(
        session,
        {
            "ccn": h.ccn,
            "name": h.name.upper(),
            "address": "1 EXAMPLE WAY",
            "city": h.city.upper(),
            "state": h.state,
            "zip": h.zip,
            "phone": h.phone,
            "hospital_type": "Acute Care Hospitals",
            "ownership": h.ownership,
            "website_domain": h.website_domain,
        },
    )
    publish_sheet(session, sheet)


def forget_cases(session: Session) -> int:
    return session.execute(delete(CaseRow)).rowcount
```

CLI additions:

```python
from waive.demo import forget_cases, seed_demo

demo_app = typer.Typer(no_args_is_help=True, help="Demo data.")
app.add_typer(demo_app, name="demo")


@demo_app.command("seed")
def demo_seed() -> None:
    """Add the fictional St. Example hospital and its policy."""
    settings = Settings()
    with session_scope(_engine(settings)) as session:
        seed_demo(session)
    console.print("Demo hospital seeded.")


@demo_app.command("forget-cases")
def demo_forget_cases() -> None:
    """Delete every case (all personal data) from the local database."""
    settings = Settings()
    with session_scope(_engine(settings)) as session:
        count = forget_cases(session)
    console.print(f"Deleted {count} cases.")
```

- [ ] **Step 3: Run, lint, commit**

```bash
uv run pytest tests/unit/test_demo.py -v
uv run ruff format . && uv run ruff check . && uv run pytest
git add src/waive/demo.py src/waive/cli.py tests/unit/test_demo.py
git commit -m "feat: demo seed and forget-cases commands"
```

- [ ] **Step 4: Phone test instructions for the user (gate U4.1; the orchestrator relays them)**

1. `uv run waive keygen` → paste the two printed lines into `.env`.
2. `uv run waive demo seed`
3. `uv run waive serve`
4. On a phone on the same Wi-Fi, open `http://<this Mac's IP>:8000` (find the IP with `ipconfig getifaddr en0`). Start a case, open the senior link, and photograph a synthetic bill shown on the laptop screen (`uv run waive corpus generate --count 3` → `var/corpus/bill-000.jpg`).
5. With `NEBIUS_API_KEY` set, the vision model reads it. The web flow sends photos with `phi=True`, so either `WAIVE_ZDR_CONFIRMED=true` must be set, or for this synthetic-only test `WAIVE_REQUIRE_ZDR=false` temporarily. Without a key, the senior sees the "reading is switched off" page.

---

### Task 4.8: Accessibility and E2E checks (optional, when time allows)

- Install: `uv add --group dev playwright axe-playwright-python` then `uv run playwright install chromium`.
- Write `tests/e2e/test_phone_flows.py` marked `@pytest.mark.e2e` and `@pytest.mark.enable_socket`: start `create_app` with the fakes from `tests/unit/test_web_senior.py` under uvicorn in a thread on `127.0.0.1:8765`, drive the senior and caregiver flows at iPhone 13 (390×844) and Pixel 7 (412×915) viewports, upload a generated bill image, and run `Axe().run(page)` on each visited page asserting no `serious` or `critical` violations.
- Register the `e2e` marker in pyproject and exclude it from the default run (`-m 'not live and not e2e'`).
- Commit: `test: phone-size E2E flows with accessibility checks`.

---

## Self-review

- **Spec coverage:** §9 steps 3 (read-back), 8 (packet), 9 (approval), 10 (reminders) → 4.2, 4.4, 4.3, 4.5; §11 capability scopes, no personal data in URLs, delete → 4.2/4.3; §13 E2E + axe → 4.8; public atlas (§7 "open, cited, versioned") → 4.6; master-plan phone test → 4.7.
- **Type consistency:** `CaseView` fields used in templates (`hospital_name, bill, tier, senior_text, caregiver_text, deadlines, candidates, warnings, needs_scouting, result, household_size, annual_income, status`) match Phase 3; `caregiver_summary` first line is the headline used by the packet; `Deadlines.collection_notice_too_early` name matches Phase 1.
- **Deviations from spec recorded:** plain CSS (no Tailwind), no HTMX, reportlab instead of WeasyPrint.
