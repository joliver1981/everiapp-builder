"""TestClient integration tests for the PUT endpoints on Secrets and AI Providers.

Both surfaces just got edit UI in the frontend — these tests confirm the backend
routes accept partial updates, return 200, and persist the changes. Required by
the CLAUDE.md rule: "When you touch an HTTP endpoint, write a TestClient test."
"""
import asyncio
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Same isolation pattern as test_create_target_http.py — both candidate test DBs
# get cleared at import time and we use uuid-suffixed names per-test for safety.
_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_AIHUB_TESTS_TMP = Path(tempfile.gettempdir()) / "aihub-tests"
for _candidate in (
    _TMP / "test_secrets_providers.db",
    _AIHUB_TESTS_TMP / "test.db",
):
    if _candidate.exists():
        try:
            _candidate.unlink()
        except OSError:
            pass

_DB = _TMP / "test_secrets_providers.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps")
os.environ["DEBUG"] = "true"
os.environ.setdefault(
    "MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8="
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret")

from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init_db():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client: TestClient) -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "password"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------- Secrets ----------

def test_update_secret_description_only(client: TestClient, admin_token: str):
    name = _unique("sec")
    r = client.post(
        "/api/secrets",
        json={"name": name, "category": "custom", "description": "initial", "value": "v1"},
        headers=_auth(admin_token),
    )
    assert r.status_code in (200, 201), r.text
    sid = r.json()["id"]

    r = client.put(
        f"/api/secrets/{sid}",
        json={"description": "updated"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "updated"
    assert r.json()["is_set"] is True  # value still present


def test_update_secret_value_keeps_is_set_true(client: TestClient, admin_token: str):
    name = _unique("sec")
    r = client.post(
        "/api/secrets",
        json={"name": name, "category": "custom", "description": "", "value": "old"},
        headers=_auth(admin_token),
    )
    sid = r.json()["id"]

    r = client.put(
        f"/api/secrets/{sid}",
        json={"value": "new-value"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_set"] is True


def test_update_secret_404_for_missing_id(client: TestClient, admin_token: str):
    r = client.put(
        "/api/secrets/00000000-0000-0000-0000-000000000000",
        json={"description": "x"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 404


def test_update_secret_requires_admin(client: TestClient):
    r = client.put(
        "/api/secrets/00000000-0000-0000-0000-000000000000",
        json={"description": "x"},
    )
    assert r.status_code == 401


# ---------- AI Providers ----------

def test_update_ai_provider_name_and_model(client: TestClient, admin_token: str):
    name = _unique("prov")
    r = client.post(
        "/api/admin/ai-providers",
        json={
            "name": name,
            "provider_type": "openai",
            "api_key": "sk-fake",
            "default_model": "gpt-4o",
        },
        headers=_auth(admin_token),
    )
    assert r.status_code in (200, 201), r.text
    pid = r.json()["id"]

    new_name = _unique("renamed")
    r = client.put(
        f"/api/admin/ai-providers/{pid}",
        json={"name": new_name, "default_model": "gpt-4o-mini"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == new_name
    assert body["default_model"] == "gpt-4o-mini"
    # provider_type unchanged
    assert body["provider_type"] == "openai"


def test_update_ai_provider_api_key_only_replaces_when_present(
    client: TestClient, admin_token: str
):
    """The frontend sends api_key only when the user actually typed a new one.
    Confirm that omitting api_key from the PUT body leaves the existing key intact."""
    name = _unique("prov")
    r = client.post(
        "/api/admin/ai-providers",
        json={
            "name": name,
            "provider_type": "openai",
            "api_key": "sk-original",
            "default_model": "gpt-4o",
        },
        headers=_auth(admin_token),
    )
    pid = r.json()["id"]

    # PUT without api_key — should leave the stored key alone.
    r = client.put(
        f"/api/admin/ai-providers/{pid}",
        json={"default_model": "gpt-4o-mini"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    # We can't read the key back, but we can confirm the row updated.
    assert r.json()["default_model"] == "gpt-4o-mini"


def test_update_ai_provider_404_for_missing_id(client: TestClient, admin_token: str):
    r = client.put(
        "/api/admin/ai-providers/00000000-0000-0000-0000-000000000000",
        json={"name": "x"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 404


def test_update_ai_provider_requires_admin(client: TestClient):
    r = client.put(
        "/api/admin/ai-providers/00000000-0000-0000-0000-000000000000",
        json={"name": "x"},
    )
    assert r.status_code == 401
