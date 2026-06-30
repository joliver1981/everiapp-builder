"""First-run setup wizard: public status, admin state checklist, completion."""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_setup_wizard.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_setup_wizard")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "setup-wizard-test")

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


def test_status_public_and_needs_setup_initially(client):
    # No auth header — status is public.
    r = client.get("/api/setup/status")
    assert r.status_code == 200
    assert r.json()["needs_setup"] is True


def test_state_requires_admin(client):
    assert client.get("/api/setup/state").status_code in (401, 403)


def test_state_checklist(client, admin_token):
    r = client.get("/api/setup/state", headers=_auth(admin_token))
    assert r.status_code == 200
    s = r.json()
    # needs_setup is still true (only this file ever flips setup_completed, and
    # that happens in a later test). The per-step booleans reflect whatever the
    # shared test engine's DB currently holds, so only assert their presence +
    # type here — test_checklist_reflects_config proves the True path directly.
    assert s["needs_setup"] is True
    for k in ("setup_completed", "has_identity_provider", "smtp_configured",
              "has_custom_prompt", "budgets_set"):
        assert k in s and isinstance(s[k], bool)


def test_complete_flips_status(client, admin_token):
    r = client.post("/api/setup/complete", headers=_auth(admin_token))
    assert r.status_code == 200 and r.json()["setup_completed"] is True

    assert client.get("/api/setup/status").json()["needs_setup"] is False
    assert client.get("/api/setup/state", headers=_auth(admin_token)).json()["needs_setup"] is False


def test_checklist_reflects_config(client, admin_token):
    # Enabling an IdP + SMTP should flip the checklist items.
    client.post("/api/admin/auth-providers", json={
        "provider_type": "ldap", "provider_name": "Setup AD",
        "config": {"server": "dc.local"}, "is_enabled": True,
    }, headers=_auth(admin_token))
    client.put("/api/admin/settings",
               json={"smtp_enabled": True, "smtp_host": "smtp.corp", "custom_system_prompt": "hi",
                     "monthly_budget_usd": 100},
               headers=_auth(admin_token))
    s = client.get("/api/setup/state", headers=_auth(admin_token)).json()
    assert s["has_identity_provider"] is True
    assert s["smtp_configured"] is True
    assert s["has_custom_prompt"] is True
    assert s["budgets_set"] is True
    # cleanup shared settings
    client.put("/api/admin/settings", json={"smtp_enabled": False}, headers=_auth(admin_token))
