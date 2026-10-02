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
            session=session,
            ai=self.ai,
            cipher=self.cipher,
            signer=self.signer,
            today=self.today_fn(),
        )


def deps_of(request: Request) -> Deps:
    return request.app.state.deps


def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    templates = deps_of(request).templates
    return templates.TemplateResponse(request, name, context, status_code=status_code)
