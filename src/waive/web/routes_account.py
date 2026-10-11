"""The account of someone signed in (Phase 9): the history of their cases, opening one of them,
and deleting the account with everything in it.

An account keeps which cases are its own, never their links: opening a case from the history
mints a fresh full link then and there, so "Make new links" still revokes every older one. The
routes do not exist while sign-in is not configured, and without a session they lead home."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from waive.auth import accounts
from waive.cases.service import full_token
from waive.db import CaseRow, session_scope
from waive.web.deps import deps_of, render
from waive.web.routes_senior import flow_links
from waive.web.signin import (
    clear_session_cookie,
    from_this_site,
    history,
    home_page,
    provider,
    signed_in,
)

router = APIRouter()


def _on(request: Request) -> None:
    if provider(request) is None:
        raise KeyError("sign-in is not configured")


def _home() -> RedirectResponse:
    """Nobody is signed in: the home page has the way in."""
    return RedirectResponse("/", status_code=303)


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    _on(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        account_id = signed_in(request, session)
        if account_id is None:
            return _home()
        return render(request, "account.html", history=history(deps.context(session), account_id))


@router.get("/account/cases/{case_id}")
def open_case(request: Request, case_id: str) -> RedirectResponse:
    """The "Open this case" button: a fresh full link for one of the account's own cases. A case
    that is not in this account is not found, whether or not it exists."""
    _on(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        account_id = signed_in(request, session)
        if account_id is None:
            return _home()
        if not accounts.is_linked(session, account_id, case_id):
            raise KeyError("no such case in this account")
        links = flow_links(full_token(deps.context(session), case_id), full=True)
        # Nothing to check yet on a case without a bill: it opens at the photo step.
        started = session.get(CaseRow, case_id).status != "new"
    return RedirectResponse(links["review" if started else "start"], status_code=303)


@router.post("/account/delete", response_class=HTMLResponse)
def delete_my_account(request: Request):
    """The "Delete my account and all my cases" button, behind its question on the account page:
    the account, its sessions in every browser and every case in its history."""
    _on(request)
    deps = deps_of(request)
    with session_scope(deps.engine) as session:
        # The session cookie alone is not enough for this one: the form must come from here.
        account_id = signed_in(request, session) if from_this_site(request) else None
        if account_id is None:
            return _home()
        accounts.delete_account(deps.context(session), account_id)
    response = home_page(request, notice="Your account and all your cases were deleted.")
    clear_session_cookie(response, request)
    # Cached copies of the case pages go with the cases, as on one-tap delete.
    response.headers["Clear-Site-Data"] = '"cache"'
    return response
