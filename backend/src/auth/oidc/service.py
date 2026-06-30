"""OIDC network operations: discovery, code exchange, id_token validation.

Discovery + token exchange use async httpx. id_token signature validation uses
PyJWT's PyJWKClient (synchronous urllib under the hood) so it runs in a thread
executor to avoid blocking the event loop.
"""
from __future__ import annotations

import asyncio

import httpx

# Cache discovery docs (they're stable) keyed by discovery URL.
_DISCOVERY_CACHE: dict[str, dict] = {}


class OidcError(Exception):
    pass


async def fetch_discovery(discovery_url: str, *, force: bool = False) -> dict:
    if not force and discovery_url in _DISCOVERY_CACHE:
        return _DISCOVERY_CACHE[discovery_url]
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(discovery_url)
        if resp.status_code >= 300:
            raise OidcError(f"discovery fetch failed: {resp.status_code}")
        doc = resp.json()
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer"):
        if required not in doc:
            raise OidcError(f"discovery doc missing {required}")
    _DISCOVERY_CACHE[discovery_url] = doc
    return doc


async def exchange_code(token_endpoint: str, *, client_id: str, client_secret: str,
                        code: str, redirect_uri: str, code_verifier: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(token_endpoint, data=data,
                                 headers={"Accept": "application/json"})
        if resp.status_code >= 300:
            raise OidcError(f"token exchange failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()


def _validate_id_token_sync(id_token: str, jwks_uri: str, *, client_id: str,
                            issuer: str, nonce: str | None) -> dict:
    import jwt
    from jwt import PyJWKClient

    signing_key = PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token, signing_key.key, algorithms=["RS256", "ES256"],
        audience=client_id, issuer=issuer,
        options={"require": ["exp", "iat", "aud", "iss"]},
    )
    if nonce is not None and claims.get("nonce") != nonce:
        raise OidcError("nonce mismatch (possible replay)")
    return claims


async def validate_id_token(id_token: str, jwks_uri: str, *, client_id: str,
                            issuer: str, nonce: str | None) -> dict:
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None, lambda: _validate_id_token_sync(
                id_token, jwks_uri, client_id=client_id, issuer=issuer, nonce=nonce)
        )
    except OidcError:
        raise
    except Exception as e:  # jwt errors, network, etc.
        raise OidcError(f"id_token validation failed: {e}")


async def fetch_userinfo(userinfo_endpoint: str, access_token: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                userinfo_endpoint, headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code >= 300:
                return {}
            return resp.json()
    except Exception:
        return {}
