"""The VM deployment files (Phase 6 step 6.6b) keep the database private and carry no secrets."""

import re
from pathlib import Path

VM = Path(__file__).resolve().parents[2] / "deploy" / "vm"


def test_compose_publishes_only_https_and_a_loopback_database():
    compose = (VM / "compose.yaml").read_text(encoding="utf-8")
    published = re.findall(r'"([0-9.:]+)"', " ".join(re.findall(r"ports: \[(.*?)\]", compose)))
    assert published == ["127.0.0.1:5432:5432", "80:80", "443:443"]
    assert "WAIVE_ENV: production" in compose
    assert "WAIVE_LEDGER_BACKEND: db" in compose
    assert "${PG_PASSWORD}" in compose
    assert "max-size" in compose  # container logs cannot fill the boot disk


def test_caddy_serves_the_names_from_the_environment():
    caddyfile = (VM / "Caddyfile").read_text(encoding="utf-8")
    assert "{$WAIVE_DOMAIN} {" in caddyfile
    assert "reverse_proxy app:8000" in caddyfile


def test_cloud_init_template_holds_no_key_and_no_secret():
    template = (VM / "cloud-init.template.yaml").read_text(encoding="utf-8")
    assert template.startswith("#cloud-config")
    assert "__SSH_PUBLIC_KEY__" in template
    assert "ssh-ed25519" not in template
    assert not re.search(r"(?i)(password|secret|token|api_key)\s*[:=]", template)
