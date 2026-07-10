"""Guard the admin Settings UI contract: every key the AdminPlatformPage save()
sends must be accepted by SettingsIn and round-trip through GET /admin/settings.

If a new setting is added to the UI but not to the backend SettingsIn schema,
the toggle would silently no-op — this test fails loudly instead.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_settings_ui.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_settings_ui")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "settings-ui-test")

from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    return client.post("/api/auth/login", json={"username": "admin", "password": "password"}).json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


# Exactly the payload AdminPlatformPage.SettingsTab.save() sends.
_UI_PAYLOAD = {
    "custom_system_prompt": "Use our palette.",
    "monthly_budget_usd": 500.0,
    "per_user_budget_usd": 50.0,
    "budget_alert_threshold": 0.75,
    "security_scan_enabled": True,
    "security_scan_block_publish": False,
    "security_scan_block_severity": "critical",
    "runtime_probe_enabled": True,
    "require_publish_approval": True,
    "auto_rollback_enabled": True,
    "auto_rollback_fail_threshold": 5,
    "siem_enabled": True,
    "siem_endpoint": "https://splunk.corp:8088/collect",
    "siem_transport": "http",
    "siem_auth_header": "Authorization: Splunk abc",
    "smtp_enabled": True,
    "smtp_host": "smtp.corp.com",
    "smtp_port": 465,
    "smtp_username": "mailer",
    "smtp_use_tls": False,
    "notify_from": "aihub@corp.com",
    "notify_admin_emails": "ops@corp.com",
    "notify_on_publish_request": True,
    "notify_on_deploy_failure": True,
    "notify_on_budget": False,
    "notify_on_bug_report": True,
    "marketplace_url": "https://marketplace.example.com",
    # Per-purpose LLM output caps + decision input cap + history window.
    "decision_max_output_tokens": 16384,
    "generation_max_output_tokens": 16384,
    "self_heal_max_output_tokens": 8192,
    "assistant_max_output_tokens": 8192,
    "bug_analysis_max_output_tokens": 8192,
    "marketplace_suggest_max_output_tokens": 2048,
    "decision_max_input_chars": 0,
    "generation_history_window": 25,
}

# smtp_password / marketplace_api_key are write-only: the UI sends them, GET scrubs.
_SECRET_KEYS = {"smtp_password", "marketplace_api_key"}


def test_ui_payload_keys_are_all_accepted_by_schema():
    """Drift guard: every key the UI save() sends must be a real SettingsIn field.
    A key present in the UI but missing from the schema would be silently dropped
    on PUT — this fails loudly instead (and is cheaper to keep in sync than the
    full round-trip list above)."""
    from src.platform_settings.router import SettingsIn
    ui_keys = set(_UI_PAYLOAD) | _SECRET_KEYS
    schema_keys = set(SettingsIn.model_fields)
    missing = ui_keys - schema_keys
    assert not missing, f"UI sends keys SettingsIn does not accept: {sorted(missing)}"


def test_all_ui_settings_round_trip(client, admin_token):
    payload = {**_UI_PAYLOAD, "smtp_password": "mailer-secret",
               "marketplace_api_key": "aihub_testkey123"}
    r = client.put("/api/admin/settings", json=payload, headers=_auth(admin_token))
    assert r.status_code == 200, r.text
    after = r.json()
    for key, value in _UI_PAYLOAD.items():
        assert after[key] == value, f"{key} did not round-trip: {after.get(key)!r} != {value!r}"
    # Secrets are accepted but scrubbed from the response.
    for key in _SECRET_KEYS:
        assert after[key] == "***REDACTED***"

    # GET reflects the same values (persisted, not just echoed)
    got = client.get("/api/admin/settings", headers=_auth(admin_token)).json()
    for key, value in _UI_PAYLOAD.items():
        assert got[key] == value, f"{key} not persisted"
    for key in _SECRET_KEYS:
        assert got[key] == "***REDACTED***"

    # Reset shared-DB-affecting toggles so other test files aren't impacted.
    client.put("/api/admin/settings", json={
        "require_publish_approval": False, "auto_rollback_enabled": False,
        "siem_enabled": False, "security_scan_block_publish": True,
        "security_scan_block_severity": "high", "smtp_enabled": False,
        "runtime_probe_enabled": False,
    }, headers=_auth(admin_token))
