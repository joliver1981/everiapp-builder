from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders

from .config import settings


class SecurityHeadersMiddleware:
    """Add baseline security response headers.

    Pure-ASGI (not BaseHTTPMiddleware) so it only touches the response-start
    headers and never buffers the body — important because the runtime proxy
    streams app responses through this app.

    Framing: routes that opt into being embedded declare their own
    `Content-Security-Policy: frame-ancestors ...` (the embed bootstrap does).
    We only add `X-Frame-Options: SAMEORIGIN` when no such CSP is present, so
    same-origin previews and the deliberate embed flow keep working while
    cross-origin clickjacking of the platform UI is blocked.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_https = scope.get("scheme") == "https"

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                csp = headers.get("Content-Security-Policy", "")
                if "frame-ancestors" not in csp:
                    headers.setdefault("X-Frame-Options", "SAMEORIGIN")
                if is_https and settings.hsts_enabled:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
            await send(message)

        await self.app(scope, receive, send_wrapper)


class TraceContextMiddleware:
    """Parse X-AIHub-Trace-Id into the trace contextvar for the request.

    Pure-ASGI like SecurityHeadersMiddleware — never buffers bodies (the
    runtime proxy streams app responses through this app). The validated id is
    echoed on the response so clients can correlate.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from .tracing.context import current_trace_id, parse_trace_id

        raw = None
        for key, value in scope.get("headers", []):
            if key == b"x-aihub-trace-id":
                raw = value.decode("latin-1")
                break
        trace_id = parse_trace_id(raw)

        async def send_wrapper(message):
            if trace_id and message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers.setdefault("X-AIHub-Trace-Id", trace_id)
            await send(message)

        token = current_trace_id.set(trace_id)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            current_trace_id.reset(token)


def setup_middleware(app: FastAPI) -> None:
    # Add SecurityHeaders first so CORS stays the outermost layer (preflight
    # short-circuits before reaching inner middleware, which is fine).
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TraceContextMiddleware)

    # Default origins from settings, plus any pattern for deployed-app callbacks.
    kwargs = dict(
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        # X-AIHub-Trace-Id: deployed apps call the platform cross-origin; the
        # SDK stamps the trace header on every request, so preflight must
        # allow it or tracing silently breaks off-platform.
        allow_headers=["Authorization", "Content-Type", "X-AIHub-Trace-Id"],
    )
    if settings.deployment_cors_allow_pattern:
        kwargs["allow_origin_regex"] = settings.deployment_cors_allow_pattern

    app.add_middleware(CORSMiddleware, **kwargs)
