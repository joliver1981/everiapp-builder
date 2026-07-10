"""Free-form external HTTP calls an app makes THROUGH a Connection.

`callConnection()` in the SDK lets a generated app make a real outbound HTTP
request (e.g. to an LLM provider or any REST API) using an admin-configured
Connection as the trust anchor: the connection supplies the base_url and the
credential (injected server-side, never seen by the app), while the app chooses
the method / relative path / query / headers / body at call time.

Guardrails (mirroring the datasets runtime):
  - the connection must be kind="rest" or kind="ai" AND `app_callable` (admin opt-in);
  - the app must be BOUND to the connection (app_connection_bindings);
  - the request path must be RELATIVE — an absolute/protocol-relative URL is
    rejected so the call can't escape the connection's base_url host;
  - the app can't override the injected auth header;
  - request/response bodies are size-capped and the call is time-bounded;
  - every call is audit-logged.
"""
from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..secrets.models import AuditLog
from .drivers import rest as rest_driver
from .models import AppConnectionBinding, Connection
from .service import connections_service

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
# Kinds whose config speaks HTTP (the REST driver) and may be app-callable.
APP_CALLABLE_KINDS = {"rest", "ai"}
MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class ExternalCallError(Exception):
    """Client-correctable problem (bad request / not allowed). Maps to 4xx."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _reserved_header_names(config: dict) -> set[str]:
    """Header names the app must NOT be able to set (they carry the credential)."""
    reserved = {"authorization", "host", "content-length"}
    if config.get("auth_type") == "api_key_header":
        reserved.add((config.get("auth_param") or "X-API-Key").lower())
    return reserved


def _reserved_query_names(config: dict) -> set[str]:
    """Query param names the app must NOT be able to set. For api_key_query the
    credential rides in a query param — httpx merges request params over the
    client's per key, so without this an app could override or blank the secret."""
    if config.get("auth_type") == "api_key_query":
        return {(config.get("auth_param") or "api_key").lower()}
    return set()


def _validate_path(path: str) -> str:
    p = (path or "").strip()
    if not p:
        raise ExternalCallError("path is required (relative to the connection's base_url)")
    low = p.lower()
    # Reject anything that would make httpx ignore the connection's base_url host.
    if "://" in low or low.startswith("//"):
        raise ExternalCallError(
            "path must be relative to the connection's base_url — "
            "an absolute or protocol-relative URL is not allowed"
        )
    return p


async def _resolve_connection(db: AsyncSession, id_or_name: str) -> Connection | None:
    """Look up a connection by its id OR its (unique) name. Apps naturally
    reference a connection by the human-readable name they gave it, not the
    UUID, so accept either — bindings are always stored/checked by the canonical
    UUID `id`."""
    return (await db.execute(select(Connection).where(
        or_(Connection.id == id_or_name, Connection.name == id_or_name)
    ))).scalar_one_or_none()


async def is_bound(db: AsyncSession, app_id: str, connection_id: str) -> bool:
    row = (await db.execute(select(AppConnectionBinding).where(
        AppConnectionBinding.app_id == app_id,
        AppConnectionBinding.connection_id == connection_id,
    ))).scalar_one_or_none()
    return row is not None


async def list_bound_connections(db: AsyncSession, app_id: str) -> list[Connection]:
    """Connection rows this app is bound to (any kind)."""
    ids = (await db.execute(select(AppConnectionBinding.connection_id).where(
        AppConnectionBinding.app_id == app_id))).scalars().all()
    if not ids:
        return []
    return list((await db.execute(
        select(Connection).where(Connection.id.in_(ids)).order_by(Connection.name)
    )).scalars().all())


async def bind_connection(db: AsyncSession, app_id: str, connection_id: str, user_id: str) -> None:
    conn = await _resolve_connection(db, connection_id)
    if not conn:
        raise ValueError("Connection not found")
    if not await is_bound(db, app_id, conn.id):
        db.add(AppConnectionBinding(app_id=app_id, connection_id=conn.id))
        db.add(AuditLog(user_id=user_id, action="app_connection.bind",
                        resource_type="connection", resource_id=conn.id,
                        details=f"Bound connection to app {app_id}"))
        await db.commit()


async def unbind_connection(db: AsyncSession, app_id: str, connection_id: str, user_id: str) -> bool:
    conn = await _resolve_connection(db, connection_id)
    cid = conn.id if conn else connection_id
    if not await is_bound(db, app_id, cid):
        return False
    await db.execute(delete(AppConnectionBinding).where(
        AppConnectionBinding.app_id == app_id,
        AppConnectionBinding.connection_id == cid,
    ))
    db.add(AuditLog(user_id=user_id, action="app_connection.unbind",
                    resource_type="connection", resource_id=cid,
                    details=f"Unbound connection from app {app_id}"))
    await db.commit()
    return True


async def execute_app_call(
    db: AsyncSession, *, app_id: str, connection_id: str, method: str,
    path: str, query: dict | None = None, headers: dict | None = None,
    body=None, user_id: str,
) -> dict:
    """Make one outbound HTTP call through a connection. Returns
    {status, headers, body}. Raises ExternalCallError for client-correctable
    problems; other exceptions surface as an upstream/transport failure."""
    conn = await _resolve_connection(db, connection_id)
    if not conn:
        raise ExternalCallError(
            f"No connection with id or name '{connection_id}'. Pass the connection's "
            f"id or its name from Admin → Connections.", status_code=404)
    if conn.kind not in APP_CALLABLE_KINDS:
        raise ExternalCallError(
            "Only REST and AI-provider connections support app calls", status_code=400)
    if not conn.app_callable:
        raise ExternalCallError(
            "This connection is not app-callable — an admin must turn on "
            "'Allow apps to call this connection' in Admin → Connections",
            status_code=403)
    if not await is_bound(db, app_id, conn.id):
        raise ExternalCallError(
            "This app isn't attached to that connection yet. In the builder open the "
            "'Data & APIs' panel → Connections → Attach, then try again.",
            status_code=403)

    m = (method or "GET").upper()
    if m not in ALLOWED_METHODS:
        raise ExternalCallError(f"method must be one of {sorted(ALLOWED_METHODS)}")
    rel_path = _validate_path(path)

    # App headers, minus anything that would clobber the injected credential.
    reserved = _reserved_header_names(conn.config or {})
    app_headers = {k: str(v) for k, v in (headers or {}).items()
                   if k.lower() not in reserved}

    # Body: dict/list → JSON; str → raw content; None → no body.
    send_kwargs: dict = {}
    if isinstance(body, (dict, list)):
        raw = _json.dumps(body)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ExternalCallError("request body too large")
        send_kwargs["json"] = body
    elif isinstance(body, str):
        if len(body) > MAX_REQUEST_BYTES:
            raise ExternalCallError("request body too large")
        send_kwargs["content"] = body
    if query:
        reserved_q = _reserved_query_names(conn.config or {})
        app_query = {k: str(v) for k, v in query.items() if k.lower() not in reserved_q}
        if app_query:
            send_kwargs["params"] = app_query
    if app_headers:
        send_kwargs["headers"] = app_headers

    secret = await connections_service.resolve_credential(db, conn.credential_secret_ref)
    timeout_s = int(conn.default_timeout_seconds or 30)
    client = rest_driver.build_client(conn.config or {}, secret=secret, timeout_seconds=timeout_s)

    status = 0
    err = None

    async def _do_call():
        # Stream so an oversized upstream body can't buffer unbounded into backend
        # memory — we stop reading once we pass the cap and flag it truncated.
        async with client.stream(m, rel_path, **send_kwargs) as resp:
            chunks: list[bytes] = []
            total = 0
            truncated = False
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    truncated = True
                    break
            raw = b"".join(chunks)[:MAX_RESPONSE_BYTES]
            return resp.status_code, dict(resp.headers), (resp.encoding or "utf-8"), raw, truncated

    try:
        status, resp_headers, encoding, raw, truncated = await asyncio.wait_for(
            _do_call(), timeout=timeout_s + 2)
        text = raw.decode(encoding, errors="replace")
        parsed = None
        if not truncated:
            try:
                parsed = _json.loads(text)
            except Exception:
                parsed = None
        result = {
            "status": status,
            "headers": resp_headers,
            "body": parsed if parsed is not None else text,
            "truncated": truncated,
        }
    except asyncio.TimeoutError:
        err = f"external call timed out after {timeout_s}s"
        raise ExternalCallError(err, status_code=504)
    except ExternalCallError:
        raise
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
        raise
    finally:
        await client.aclose()
        db.add(AuditLog(
            user_id=user_id, action="app_connection.call",
            resource_type="connection", resource_id=connection_id,
            details=f"app={app_id} {m} {rel_path} -> {status or err}",
        ))
        await db.commit()

    return result
