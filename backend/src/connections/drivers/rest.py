"""httpx.AsyncClient builder for REST connections.

Supports a small set of auth schemes the credential_secret_ref can feed:
  - none
  - bearer            -> Authorization: Bearer <secret>
  - basic             -> Authorization: Basic base64(secret)  (secret is "user:pass")
  - api_key_header    -> <header_name>: <secret>              (header_name from config)
  - api_key_query     -> ?<param_name>=<secret>               (param_name from config)
"""
from __future__ import annotations

import base64
from typing import Optional

import httpx

AUTH_TYPES = {"none", "bearer", "basic", "api_key_header", "api_key_query"}


def build_client(
    config: dict,
    *,
    secret: Optional[str] = None,
    timeout_seconds: int = 30,
) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient for the connection.

    `config` keys:
      base_url:        required
      auth_type:       one of AUTH_TYPES (default "none")
      default_headers: dict of static headers
      default_query:   dict of static query params (e.g. Azure OpenAI's api-version)
      auth_param:      header name (api_key_header) or query param name (api_key_query)
    """
    base_url = config.get("base_url")
    if not base_url:
        raise ValueError("config.base_url is required")

    auth_type = config.get("auth_type", "none")
    if auth_type not in AUTH_TYPES:
        raise ValueError(f"Unknown auth_type '{auth_type}'. Known: {sorted(AUTH_TYPES)}")

    headers: dict[str, str] = dict(config.get("default_headers") or {})
    # Static params first, so an api_key_query credential overwrites a collision.
    params: dict[str, str] = {
        str(k): str(v) for k, v in (config.get("default_query") or {}).items()
    }

    if secret:
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif auth_type == "basic":
            # Expect secret as "user:pass"
            encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        elif auth_type == "api_key_header":
            header_name = config.get("auth_param") or "X-API-Key"
            headers[header_name] = secret
        elif auth_type == "api_key_query":
            param_name = config.get("auth_param") or "api_key"
            params[param_name] = secret

    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        params=params,
        timeout=timeout_seconds,
    )
