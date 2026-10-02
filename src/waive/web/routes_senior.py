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
