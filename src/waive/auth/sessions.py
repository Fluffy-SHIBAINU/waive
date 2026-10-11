"""Signed-in sessions: a random id in the browser's cookie, a row on the server.

The cookie holds 32 random bytes and nothing else. The table keeps the SHA-256 of that id, so
reading the table does not hand out live sessions, and a lookup is by the whole hash: there is
no comparison of the secret itself whose timing could be measured."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from waive.db import SessionRow, utcnow

LIFETIME = timedelta(days=14)
ID_BYTES = 32
# `secrets.token_urlsafe(32)` is 43 characters; anything much longer was never issued here.
MAX_ID_CHARS = 128


def _stored(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive timestamps; they were written in UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def create(session: Session, account_id: int, now: datetime | None = None) -> str:
    """Start a session for the account and return the id for the cookie."""
    now = now or utcnow()
    session_id = secrets.token_urlsafe(ID_BYTES)
    session.add(
        SessionRow(
            id=_stored(session_id),
            account_id=account_id,
            created_at=now,
            expires_at=now + LIFETIME,
        )
    )
    session.flush()
    return session_id


def read(session: Session, session_id: str, now: datetime | None = None) -> int | None:
    """The account a session id belongs to, or None when it is unknown, ended or expired."""
    if not session_id or len(session_id) > MAX_ID_CHARS:
        return None
    row = session.get(SessionRow, _stored(session_id))
    if row is None or _aware(row.expires_at) <= (now or utcnow()):
        return None
    return row.account_id


def end(session: Session, session_id: str) -> None:
    """Sign out: the id stops working at once. Ending an unknown session is not an error."""
    if not session_id or len(session_id) > MAX_ID_CHARS:
        return
    session.execute(delete(SessionRow).where(SessionRow.id == _stored(session_id)))
    session.flush()


def purge_expired(session: Session, now: datetime | None = None) -> int:
    """Delete the sessions that ran out; returns how many."""
    # Not matched against rows already loaded: SQLite's naive timestamps cannot be compared
    # with `now` in Python, and `read` checks the expiry of whatever it finds anyway.
    gone = session.execute(
        delete(SessionRow)
        .where(SessionRow.expires_at <= (now or utcnow()))
        .execution_options(synchronize_session=False)
    )
    session.flush()
    return gone.rowcount
