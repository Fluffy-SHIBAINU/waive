import base64
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from waive.cases.vault import FieldCipher, TokenSigner, cipher_from_settings, new_key
from waive.config import Settings
from waive.db import CaseRow, init_db, make_engine, session_scope


def test_cipher_round_trip_and_tamper_detection():
    key = base64.b64decode(new_key())
    cipher = FieldCipher(key)
    blob = cipher.encrypt({"patient_name": "Rosa Alvarez", "amount_due": "1850.00"})
    assert "Rosa" not in blob
    assert cipher.decrypt(blob) == {"patient_name": "Rosa Alvarez", "amount_due": "1850.00"}
    tampered = blob[:-2] + ("AA" if blob[-2:] != "AA" else "BB")
    with pytest.raises(ValueError):
        cipher.decrypt(tampered)


def test_cipher_from_settings_requires_key():
    with pytest.raises(RuntimeError):
        cipher_from_settings(Settings(_env_file=None))
    settings = Settings(_env_file=None, vault_key=SecretStr(new_key()))
    assert isinstance(cipher_from_settings(settings), FieldCipher)


def test_tokens_carry_scope_and_expire():
    signer = TokenSigner("s" * 32)
    now = datetime(2026, 10, 2, 12, tzinfo=UTC)
    token = signer.mint("case1", "caregiver", 1, timedelta(days=30), now=now)
    claims = signer.verify(token, now=now + timedelta(days=1))
    assert (claims.case_id, claims.scope, claims.generation) == ("case1", "caregiver", 1)
    assert signer.verify(token, now=now + timedelta(days=31)) is None
    assert signer.verify(token + "x", now=now) is None
    assert TokenSigner("t" * 32).verify(token, now=now) is None


def test_case_row_persists_sealed_blob():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        session.add(CaseRow(id="abc", state="MA", status="new", token_generation=1, sealed="blob"))
    with session_scope(engine) as session:
        row = session.get(CaseRow, "abc")
        assert (row.sealed, row.prediction, row.ccn) == ("blob", None, None)
