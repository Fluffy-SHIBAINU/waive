"""`waive keygen` output is pasted into `.env`, so each value must sit on one line. Rich hard-wraps
at 80 columns when stdout is not a terminal, which silently split the 107-character
`WAIVE_TOKEN_SECRET` line (security audit finding secrets-config-6)."""

from typer.testing import CliRunner

from waive.cli import app

KEY_LEN = 44  # base64 of 32 random bytes
SECRET_LEN = 2 * KEY_LEN  # two keys, 107 characters with the name: wider than an 80-column terminal


def test_keygen_prints_each_value_on_its_own_unwrapped_line():
    result = CliRunner().invoke(app, ["keygen"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert len(lines) == 2, lines
    vault, secret = lines
    assert vault.startswith("WAIVE_VAULT_KEY=")
    assert len(vault) == len("WAIVE_VAULT_KEY=") + KEY_LEN
    assert secret.startswith("WAIVE_TOKEN_SECRET=")
    assert len(secret) == len("WAIVE_TOKEN_SECRET=") + SECRET_LEN
    for line in lines:
        assert " " not in line and "\t" not in line, line


def test_keygen_values_differ_between_runs():
    first = CliRunner().invoke(app, ["keygen"]).output
    second = CliRunner().invoke(app, ["keygen"]).output
    assert first != second
