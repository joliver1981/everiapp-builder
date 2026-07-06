"""Embed config helpers + signed embed-token mint/verify."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import jwt

from ..config import settings

_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$")
# Session-length, matching the preview token: the embedded page has no
# refresh path, and an in-iframe reload re-presents the token from its URL —
# a short TTL would strand embedded apps unauthenticated mid-session.
EMBED_TOKEN_TTL_SECONDS = 12 * 3600


def parse_origins(csv: str) -> list[str]:
    return [o.strip() for o in (csv or "").split(",") if o.strip()]


def validate_origins(origins: list[str]) -> list[str]:
    """Return the cleaned list, raising ValueError on the first malformed origin."""
    cleaned = []
    for o in origins:
        o = o.strip().rstrip("/")
        if not o:
            continue
        if o != "*" and not _ORIGIN_RE.match(o):
            raise ValueError(
                f"Invalid origin '{o}'. Use scheme://host[:port] (e.g. https://portal.acme.com)."
            )
        cleaned.append(o)
    return cleaned


def frame_ancestors(origins: list[str]) -> str:
    """Build the CSP frame-ancestors directive value. Empty list → wildcard."""
    if not origins:
        return "*"
    # 'self' is always allowed so the platform's own viewer keeps working.
    return "'self' " + " ".join(origins)


def mint_embed_token(app_id: str) -> tuple[str, int]:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": app_id,
        "typ": "embed",
        "iat": now,
        "exp": now + timedelta(seconds=EMBED_TOKEN_TTL_SECONDS),
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, EMBED_TOKEN_TTL_SECONDS


def verify_embed_token(token: str) -> str | None:
    """Return the app_id if the token is a valid, unexpired embed token, else None."""
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "embed":
        return None
    return payload.get("sub")


def iframe_snippet(embed_url: str) -> str:
    return (
        f'<iframe src="{embed_url}" '
        f'style="width:100%;height:600px;border:0" '
        f'allow="clipboard-read; clipboard-write" '
        f'title="AIHub app"></iframe>'
    )
