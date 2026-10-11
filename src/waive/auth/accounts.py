"""Accounts: who signed in and which cases are theirs (Phase 9 privacy rules).

An account is a provider's name and a keyed hash of that provider's subject id. No email, name
or picture is asked for or stored. An account keeps which cases are its own, never their links;
deleting the account deletes every one of those cases."""

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from waive.cases.service import CaseContext, delete_case
from waive.cases.vault import TokenSigner, keyed_hash
from waive.db import AccountCaseRow, AccountRow, CaseRow, SessionRow

GOOGLE = "google"


def subject_message(provider: str, subject: str) -> bytes:
    """What is hashed for one person at one provider. The zero bytes keep the provider and the
    subject apart, and keep the message apart from a case link's signed part, which the same
    secret signs and which never holds one."""
    return b"\x00".join((b"waive-account", provider.encode("utf-8"), subject.encode("utf-8")))


def subject_hash(secret: str, provider: str, subject: str) -> str:
    """HMAC-SHA256 (hex) of the provider's subject id, keyed with the token secret: the same
    person always gets the same account, and the table alone cannot be turned back into, or
    matched against, anyone's subject id."""
    return keyed_hash(secret.encode("utf-8"), subject_message(provider, subject))


def signer_subject_hash(signer: TokenSigner, provider: str, subject: str) -> str:
    """`subject_hash` for the web app, whose token secret lives inside its link signer."""
    return signer.keyed_hash(subject_message(provider, subject))


def get_or_create(session: Session, provider: str, digest: str) -> AccountRow:
    """The account for a subject hash, made on the first sign-in. Call it before any other write
    in the transaction: when two first sign-ins of one person race, the loser rolls back and
    takes the winner's row."""
    query = select(AccountRow).where(
        AccountRow.provider == provider, AccountRow.subject_hash == digest
    )
    found = session.scalar(query)
    if found is not None:
        return found
    row = AccountRow(provider=provider, subject_hash=digest)
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return session.scalars(query).one()
    return row


def is_linked(session: Session, account_id: int, case_id: str) -> bool:
    return session.get(AccountCaseRow, (account_id, case_id)) is not None


def link_case(session: Session, account_id: int, case_id: str) -> bool:
    """Keep a case in the account's history; True when it was not there yet. The caller has
    already shown it may open the case (its full link, or it just started it)."""
    if session.get(CaseRow, case_id) is None:
        raise KeyError(case_id)
    if is_linked(session, account_id, case_id):
        return False
    session.add(AccountCaseRow(account_id=account_id, case_id=case_id))
    session.flush()
    return True


def cases_of(session: Session, account_id: int) -> list[CaseRow]:
    """The account's cases, newest first."""
    query = (
        select(CaseRow)
        .join(AccountCaseRow, AccountCaseRow.case_id == CaseRow.id)
        .where(AccountCaseRow.account_id == account_id)
        .order_by(CaseRow.created_at.desc(), CaseRow.id)
    )
    return list(session.scalars(query))


def delete_account(ctx: CaseContext, account_id: int) -> int:
    """Delete the account, its sessions and every case linked to it, each with the same one-tap
    delete its own page offers. Returns the number of cases deleted."""
    session = ctx.session
    case_ids = [row.id for row in cases_of(session, account_id)]
    for case_id in case_ids:
        delete_case(ctx, case_id)
    session.execute(delete(SessionRow).where(SessionRow.account_id == account_id))
    session.execute(delete(AccountCaseRow).where(AccountCaseRow.account_id == account_id))
    session.execute(delete(AccountRow).where(AccountRow.id == account_id))
    session.flush()
    return len(case_ids)
