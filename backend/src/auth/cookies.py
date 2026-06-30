"""Centralized refresh-cookie handling.

Every place that issues a session (password login, token refresh, SAML ACS, OIDC
callback) writes the same httpOnly refresh cookie. Keeping the attributes in one
helper means the Secure flag, SameSite, lifetime, and path can't drift between
call sites — they did before, and the Secure flag was simply never set.
"""
from __future__ import annotations

from fastapi import Request, Response

from ..config import settings

REFRESH_COOKIE = "refresh_token"


def cookie_secure(request: Request) -> bool:
    """Whether to set the Secure flag, honoring the explicit setting or auto-detecting.

    Auto mode marks the cookie Secure when the request arrived over HTTPS, which is
    correct for both a plain-HTTP lab and a TLS/reverse-proxy production deployment
    (uvicorn --proxy-headers / X-Forwarded-Proto makes the scheme show as https).
    """
    if settings.cookie_secure is not None:
        return settings.cookie_secure
    return request.url.scheme == "https"


def set_refresh_cookie(request: Request, response: Response, value: str) -> None:
    """Write the refresh-token cookie with consistent, hardened attributes."""
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=value,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(request),
        # Match the cookie lifetime to the server-side refresh-token TTL so an idle
        # session doesn't silently drop the cookie while the token is still valid.
        max_age=settings.jwt_refresh_token_expire_days * 24 * 60 * 60,
        path="/api/auth",
    )


def clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth")
