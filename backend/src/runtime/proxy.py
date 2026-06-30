"""Reverse proxy for running apps — forwards HTTP & WebSocket to Vite dev servers."""
import logging

import httpx
from fastapi import Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from ..config import settings

logger = logging.getLogger(__name__)

# Persistent client for proxying
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _http_client


async def close_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


def _inject_context(html: str, app_id: str, user: dict | None, token: str | None) -> str:
    """Inject window globals into the HTML for the app SDK."""
    import json

    script_parts = [f'window.__AIHUB_APP_ID__ = "{app_id}";']
    if user:
        script_parts.append(f"window.__AIHUB_USER__ = {json.dumps(user)};")
    if token:
        script_parts.append(f'window.__AIHUB_TOKEN__ = "{token}";')

    inject_tag = f"<script>{' '.join(script_parts)}</script>"

    # Insert after <head>
    if "<head>" in html:
        return html.replace("<head>", f"<head>{inject_tag}", 1)
    elif "<head " in html:
        idx = html.index("<head ")
        close = html.index(">", idx)
        return html[: close + 1] + inject_tag + html[close + 1 :]
    else:
        return inject_tag + html


async def proxy_http(
    request: Request,
    port: int,
    app_id: str,
    path: str,
    user: dict | None = None,
    token: str | None = None,
) -> Response:
    """Forward an HTTP request to the Vite dev server and return the response.

    Vite runs with `base=/apps/{app_id}/` (set by the runtime manager) so its assets + HMR live
    under that prefix — so we forward the FULL base-prefixed path, NOT a stripped one, or Vite
    404s every asset (/@vite/client, /src/main.tsx, …).
    """
    target_url = f"http://127.0.0.1:{port}/apps/{app_id}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    client = _get_client()

    # Build headers (strip host, add forwarded info)
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("connection", None)

    body = await request.body()

    try:
        resp = await client.request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )
    except httpx.ConnectError:
        return Response(
            content="App is not running or still starting up",
            status_code=502,
            media_type="text/plain",
        )

    content = resp.content
    content_type = resp.headers.get("content-type", "")

    # Inject context into HTML responses
    if "text/html" in content_type:
        html = content.decode("utf-8", errors="replace")
        html = _inject_context(html, app_id, user, token)
        content = html.encode("utf-8")

    # Build response headers
    resp_headers = {}
    for key, value in resp.headers.items():
        lower = key.lower()
        if lower not in ("transfer-encoding", "content-encoding", "content-length", "connection"):
            resp_headers[key] = value

    # Let the platform UI FRAME this preview. Declaring frame-ancestors makes
    # SecurityHeadersMiddleware skip X-Frame-Options. In production the SPA is
    # same-origin so 'self' covers it; in split-origin dev the builder runs at
    # :5173 while the preview is served from :8800, so without this the in-builder
    # Preview iframe is blank ("localhost refused to connect"). Only applied to the
    # framed HTML document.
    if "text/html" in content_type:
        cors = [o for o in (settings.cors_origins or []) if o and o != "*"]
        frame_src = ["'self'", *cors]
        if settings.debug:
            frame_src += ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174"]
        resp_headers["Content-Security-Policy"] = "frame-ancestors " + " ".join(dict.fromkeys(frame_src))

    return Response(
        content=content,
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=content_type.split(";")[0] if content_type else None,
    )


async def proxy_websocket(ws: WebSocket, port: int, path: str) -> None:
    """Relay WebSocket messages between client and Vite HMR server."""
    import websockets

    await ws.accept()
    target_url = f"ws://127.0.0.1:{port}/{path}"

    try:
        async with websockets.connect(target_url) as upstream:

            async def client_to_upstream():
                try:
                    while True:
                        data = await ws.receive_text()
                        await upstream.send(data)
                except WebSocketDisconnect:
                    pass

            async def upstream_to_client():
                try:
                    async for message in upstream:
                        if isinstance(message, str):
                            await ws.send_text(message)
                        else:
                            await ws.send_bytes(message)
                except Exception:
                    pass

            import asyncio
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        logger.debug("WebSocket proxy error for port %d: %s", port, e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
