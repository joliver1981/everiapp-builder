"""OIDC SSO — pure helpers + a full authorization-code round-trip against a
mock IdP (local HTTP server serving discovery / JWKS / token / userinfo).

Because OIDC is just httpx + PyJWT (no xmlsec), the entire login flow is
exercised here: login redirect → code exchange → id_token JWKS validation →
nonce check → claim→role → provision → issued access token.
"""
from __future__ import annotations

import http.server
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import asyncio

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

_TMP = Path(tempfile.gettempdir()) / "aihub-integration"
_TMP.mkdir(parents=True, exist_ok=True)
_DB = _TMP / "test_oidc.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ["APP_DATA_DIR"] = str(_TMP / "apps_oidc")
os.environ["DEBUG"] = "true"
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ["JWT_SECRET_KEY"] = "oidc-test-secret-key-1234567890"

from src.auth.oidc import client as oc  # noqa: E402
from src.config import settings  # noqa: E402
from src.database import init_db  # noqa: E402
from src.main import app  # noqa: E402

# Use the secret the PLATFORM actually signs with. settings.jwt_secret_key is
# bound at first config import, which in the full suite is some other test
# file's value — decoding app-issued tokens with os.environ here would fail.
_JWT_SECRET = settings.jwt_secret_key

# --- RSA signing key + JWKS for the mock IdP -------------------------------
_PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIV_PEM = _PRIV.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
_JWK = json.loads(RSAAlgorithm.to_jwk(_PRIV.public_key()))
_JWK.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
_JWKS = {"keys": [_JWK]}


def _make_id_token(nonce: str, issuer: str, aud: str, extra: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": issuer, "sub": "oidc-user-1", "aud": aud,
        "iat": now, "exp": now + timedelta(hours=1), "nonce": nonce, **extra,
    }
    return jwt.encode(payload, _PRIV_PEM, algorithm="RS256", headers={"kid": "test-key"})


class _Idp(http.server.BaseHTTPRequestHandler):
    discovery: dict = {}
    id_token: str | None = None
    userinfo: dict = {}

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/.well-known/openid-configuration"):
            self._json(_Idp.discovery)
        elif self.path.startswith("/jwks"):
            self._json(_JWKS)
        elif self.path.startswith("/userinfo"):
            self._json(_Idp.userinfo)
        else:
            self._json({"error": "not_found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path.startswith("/token"):
            self._json({"id_token": _Idp.id_token, "access_token": "mock-at", "token_type": "Bearer"})
        else:
            self._json({"error": "not_found"}, 404)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


@pytest.fixture(scope="module")
def idp():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Idp)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    issuer = f"http://127.0.0.1:{port}"
    _Idp.discovery = {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "jwks_uri": f"{issuer}/jwks",
        "userinfo_endpoint": f"{issuer}/userinfo",
    }
    yield port
    srv.shutdown()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_token(client):
    return client.post("/api/auth/login", json={"username": "admin", "password": "password"}).json()["access_token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_pkce_pair_is_valid():
    import base64
    import hashlib
    v, c = oc.pkce_pair()
    assert v != c and len(v) >= 43
    expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    assert c == expected


def test_build_authorize_url():
    url = oc.build_authorize_url("https://idp/authorize", client_id="cid",
                                 redirect_uri="https://x/cb", scopes="openid email",
                                 state="st", nonce="nn", code_challenge="cc")
    q = parse_qs(urlparse(url).query)
    assert q["client_id"] == ["cid"] and q["code_challenge_method"] == ["S256"]
    assert q["state"] == ["st"] and q["nonce"] == ["nn"]


def test_state_cookie_round_trip():
    tok = oc.encode_state(_JWT_SECRET, provider_id="p1", nonce="n", code_verifier="v",
                          return_to="/x", state="s")
    data = oc.decode_state(_JWT_SECRET, tok)
    assert data and data["pid"] == "p1" and data["nonce"] == "n"
    assert oc.decode_state("wrong-secret", tok) is None


def test_extract_identity_pipe_fallback():
    res = oc.extract_identity({"sub": "abc", "email": "e@x.com", "name": "Jane"}, None)
    assert res.username == "e@x.com"  # preferred_username missing → email
    assert res.external_id == "abc"
    assert res.display_name == "Jane"


# ---------------------------------------------------------------------------
# Routes + full round-trip
# ---------------------------------------------------------------------------
def _create_provider(client, admin_token, idp_port, **overrides) -> dict:
    config = {
        "discovery_url": f"http://127.0.0.1:{idp_port}/.well-known/openid-configuration",
        "client_id": "client-123", "client_secret": "super-secret",
        "scopes": "openid email profile",
    }
    config.update(overrides.pop("config", {}))
    body = {
        "provider_type": "oidc", "provider_name": f"OIDC {uuid.uuid4().hex[:5]}",
        "config": config, "group_role_mapping": {"Engineers": "developer"},
        "default_role": "user", "auto_provision": True, "is_enabled": True, "is_default": False,
    }
    body.update(overrides)
    r = client.post("/api/admin/auth-providers", json=body, headers=_auth(admin_token))
    assert r.status_code == 201, r.text
    return r.json()


def test_client_secret_scrubbed(client, admin_token, idp):
    created = _create_provider(client, admin_token, idp)
    assert created["config"]["client_secret"] == "***REDACTED***"
    assert "super-secret" not in json.dumps(created)


def test_public_provider_list(client, admin_token, idp):
    _create_provider(client, admin_token, idp)
    r = client.get("/api/auth/oidc/providers")
    assert r.status_code == 200 and len(r.json()) >= 1
    assert all(set(p) == {"id", "name"} for p in r.json())


def test_full_oidc_login(client, admin_token, idp):
    prov = _create_provider(client, admin_token, idp)
    pid = prov["id"]

    # 1. Login → 302 to the IdP authorize endpoint, state cookie set.
    r = client.get(f"/api/auth/oidc/{pid}/login", follow_redirects=False)
    assert r.status_code == 302, r.text
    authz = urlparse(r.headers["location"])
    qs = parse_qs(authz.query)
    state = qs["state"][0]
    state_cookie = r.cookies.get("oidc_state")
    assert state_cookie
    nonce = jwt.decode(state_cookie, _JWT_SECRET, algorithms=["HS256"])["nonce"]

    # 2. Stage the IdP's token + userinfo (groups drive the role).
    _Idp.id_token = _make_id_token(
        nonce, _Idp.discovery["issuer"], "client-123",
        {"email": "eng@corp.com", "name": "Eng User", "preferred_username": "enguser"})
    _Idp.userinfo = {"groups": ["Engineers"]}

    # 3. IdP redirects back to our callback with code + state.
    r = client.get(f"/api/auth/oidc/{pid}/callback?code=authcode&state={state}",
                   follow_redirects=False)
    assert r.status_code == 302, r.text
    frag = r.headers["location"].split("#", 1)[1]
    access_token = parse_qs(frag)["access_token"][0]
    payload = jwt.decode(access_token, _JWT_SECRET, algorithms=["HS256"])
    assert payload["role"] == "developer"  # Engineers → developer

    # The user was provisioned with the OIDC identity.
    me = client.get("/api/auth/me", headers=_auth(access_token)).json()
    assert me["user"]["username"] == "enguser"
    assert me["user"]["email"] == "eng@corp.com"
    assert me["user"]["role"] == "developer"


def test_callback_bad_state_redirects_with_error(client, admin_token, idp):
    prov = _create_provider(client, admin_token, idp)
    # No state cookie / wrong state → error redirect, no crash.
    r = client.get(f"/api/auth/oidc/{prov['id']}/callback?code=x&state=bogus",
                   follow_redirects=False)
    assert r.status_code == 302
    assert "oidc_error=state_mismatch" in r.headers["location"]


def test_login_invalid_config_400(client, admin_token, idp):
    prov = _create_provider(client, admin_token, idp, config={"discovery_url": ""})
    r = client.get(f"/api/auth/oidc/{prov['id']}/login", follow_redirects=False)
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "oidc_config_invalid"


def test_unknown_provider_404(client):
    assert client.get("/api/auth/oidc/nope/login", follow_redirects=False).status_code == 404
