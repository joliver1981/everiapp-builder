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


def _frame_ancestors_value() -> str:
    """CSP frame-ancestors for preview responses. Declaring it makes
    SecurityHeadersMiddleware skip X-Frame-Options, which is what lets the
    split-origin dev builder (:5173) frame the preview (:8800)."""
    cors = [o for o in (settings.cors_origins or []) if o and o != "*"]
    frame_src = ["'self'", *cors]
    if settings.debug:
        frame_src += ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5174"]
    return "frame-ancestors " + " ".join(dict.fromkeys(frame_src))


def retry_page(reason: str, status_code: int = 502) -> Response:
    """Self-healing error page for the preview iframe.

    Every proxy failure MUST come back as text/html WITH frame-ancestors:
    a JSON/plain-text error gets X-Frame-Options SAMEORIGIN from
    SecurityHeadersMiddleware, and the cross-origin builder iframe then renders
    it as a silent blank page — the "blank preview until Reload iframe" bug.
    The page polls its own URL and reloads the moment the app answers, so a
    transient failure at first mount heals itself instead of sticking forever.
    """
    import html as _html

    safe_reason = _html.escape(reason)
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Starting…</title>
<style>
  html,body {{ height:100%; margin:0; font-family:system-ui,sans-serif;
               background:#0b0d10; color:#9aa4b2; }}
  .wrap {{ height:100%; display:flex; flex-direction:column; gap:14px;
           align-items:center; justify-content:center; text-align:center; }}
  .spin {{ width:26px; height:26px; border-radius:50%;
           border:3px solid #2a313c; border-top-color:#7aa2f7;
           animation:r 0.9s linear infinite; }}
  @keyframes r {{ to {{ transform:rotate(360deg); }} }}
  p {{ margin:0; font-size:13px; max-width:340px; line-height:1.5; }}
  button {{ display:none; margin-top:6px; padding:6px 14px; font-size:12px;
            border-radius:8px; border:1px solid #2a313c; background:#161b22;
            color:#c9d1d9; cursor:pointer; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="spin"></div>
  <p>{safe_reason}</p>
  <p style="font-size:11px;opacity:.7">This page retries automatically.</p>
  <button id="retry" onclick="location.reload()">Try again</button>
</div>
<script>
  (function () {{
    var started = Date.now();
    function ping() {{
      fetch(location.href, {{ cache: 'no-store' }})
        .then(function (r) {{
          var html = (r.headers.get('content-type') || '').indexOf('text/html') !== -1;
          if (r.ok && html) {{ location.reload(); return; }}
          again();
        }})
        .catch(again);
    }}
    function again() {{
      if (Date.now() - started < 120000) {{ setTimeout(ping, 1500); }}
      else {{
        document.getElementById('retry').style.display = 'inline-block';
        document.querySelector('.spin').style.display = 'none';
      }}
    }}
    setTimeout(ping, 1500);
  }})();
</script>
</body>
</html>"""
    return Response(
        content=body,
        status_code=status_code,
        media_type="text/html",
        headers={
            "Content-Security-Policy": _frame_ancestors_value(),
            # Never cache — the iframe must get a fresh answer once the app is up.
            "Cache-Control": "no-store",
        },
    )


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
    except httpx.HTTPError as e:
        # Catch EVERYTHING httpx can throw, not just ConnectError — a cold-start
        # ReadTimeout used to escape as an unframable 500 and blank the preview.
        logger.info("proxy to app %s failed (%s: %s)", app_id, type(e).__name__, e)
        return retry_page("The app is still starting up — hang tight.")

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
        resp_headers["Content-Security-Policy"] = _frame_ancestors_value()

    return Response(
        content=content,
        status_code=resp.status_code,
        headers=resp_headers,
        media_type=content_type.split(";")[0] if content_type else None,
    )


async def proxy_websocket(ws: WebSocket, port: int, path: str) -> None:
    """Relay WebSocket messages between client and Vite HMR server."""
    import websockets

    # Negotiate the subprotocol END-TO-END. Vite's HMR client connects with
    # 'vite-hmr'; if our 101 response doesn't echo it back, the browser fails
    # the whole connection (per spec) and Vite falls back to a direct socket
    # to the raw Vite port — which only works when that port is reachable from
    # the browser. Forward whatever the client asked for to Vite, and echo
    # Vite's pick back to the client.
    requested = list(ws.scope.get("subprotocols") or [])
    target_url = f"ws://127.0.0.1:{port}/{path}"
    accepted = False

    try:
        async with websockets.connect(
            target_url,
            subprotocols=requested or None,
            # Be as transparent as a browser, which never pings and accepts any
            # frame size. The library defaults (20s ping/20s timeout, 1MB frame
            # cap) can kill this socket mid-session — a Vite stall during a big
            # transform, or one oversized HMR payload — and Vite's client reacts
            # to ANY reconnect with location.reload(): the running preview
            # "randomly restarts" under the user.
            ping_interval=None,
            max_size=None,
        ) as upstream:
            await ws.accept(subprotocol=upstream.subprotocol)
            accepted = True

            async def client_to_upstream():
                try:
                    while True:
                        data = await ws.receive_text()
                        await upstream.send(data)
                except WebSocketDisconnect:
                    pass
                except Exception:
                    # Upstream refused the send (closing) — let the other
                    # direction surface the close instead of crashing the relay.
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
        if not accepted:
            # Upstream unreachable while the manager still says "running"
            # (crashed Vite, stale status): same storm risk as the router's
            # not-running path — a pre-accept close is an HTTP 403 rejection
            # that Vite's once-a-second reconnect pings retry forever. Accept
            # so the ping "succeeds" and the page reloads into retry_page.
            try:
                await ws.accept(subprotocol=requested[0] if requested else None)
            except Exception:
                pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
