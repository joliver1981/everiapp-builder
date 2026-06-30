"""Pure OIDC helpers — no network. Fully unit-testable.

PKCE generation, authorize-URL building, signed-state cookie encode/decode, and
claim→identity mapping. Role resolution is shared via ..providers.roles.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt

from ..providers.base import AuthResult

STATE_TTL_SECONDS = 600  # 10 min to complete the round-trip

_DEFAULT_ATTR_MAP = {
    # Pipe-separated fallbacks: first present claim wins.
    "username": "preferred_username|email|sub",
    "email": "email",
    "display_name": "name|preferred_username",
    "groups": "groups",
}


def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorize_url(authorization_endpoint: str, *, client_id: str, redirect_uri: str,
                        scopes: str, state: str, nonce: str, code_challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scopes or "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


def encode_state(secret: str, *, provider_id: str, nonce: str, code_verifier: str,
                 return_to: str | None, state: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "pid": provider_id, "nonce": nonce, "cv": code_verifier,
        "rt": return_to or "", "st": state,
        "iat": now, "exp": now + timedelta(seconds=STATE_TTL_SECONDS),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_state(secret: str, token: str) -> dict | None:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def _claim(claims: dict, spec: str):
    """Resolve a possibly pipe-separated claim spec to the first present value."""
    for key in spec.split("|"):
        key = key.strip()
        if key in claims and claims[key] not in (None, "", []):
            return claims[key]
    return None


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def extract_identity(claims: dict, mapping: dict | None) -> AuthResult:
    """Map OIDC id_token / userinfo claims to an AuthResult."""
    m = {**_DEFAULT_ATTR_MAP, **(mapping or {})}
    sub = claims.get("sub") or ""
    username = _claim(claims, m["username"]) or sub
    email = _claim(claims, m["email"])
    display = _claim(claims, m["display_name"]) or username
    groups = _as_list(_claim(claims, m["groups"]))
    external_id = str(sub or username)
    return AuthResult(
        success=bool(username),
        username=str(username),
        display_name=str(display or username),
        email=str(email) if email else None,
        external_id=external_id,
        groups=groups,
    )


def validate_oidc_config(config: dict) -> list[str]:
    problems = []
    for key, label in (("discovery_url", "Discovery URL"),
                       ("client_id", "Client ID")):
        if not (config.get(key) or "").strip():
            problems.append(f"Missing {label} ({key})")
    return problems
