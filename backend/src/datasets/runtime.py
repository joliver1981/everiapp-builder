"""Runtime executor — the layer deployed apps actually call.

Apps never see connection strings; they POST params to
  /api/apps/{app_id}/datasets/{dataset_id}/execute
authenticated as the calling user (JWT), and this module:

  1. Confirms an `app_dataset_bindings` row exists for (app_id, dataset_id).
  2. Resolves the connection + credentials from the Secrets store.
  3. Injects `:current_user` into every SQL execution.
  4. Enforces a per-call row limit (dataset override or connection default,
     hard-capped) by fetching `limit + 1` rows and surfacing `truncated`.
  5. Enforces a per-call timeout via SQLAlchemy / httpx.
  6. Writes an `audit_logs` row keyed on `dataset_id`.

This intentionally reuses the same SQL/REST execution code paths as preview,
but with the *real* (not preview-capped) row limit + timeout, plus
:current_user injection and binding enforcement.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ..connections.drivers import rest as rest_driver
from ..connections.drivers import sql as sql_driver
from ..connections.models import Connection
from ..connections.service import connections_service
from ..secrets.models import AuditLog
from .models import AppDatasetBinding, Dataset
from .schemas import DatasetPreviewColumn, DatasetPreviewResult
from .service import _coerce_for_json, _substitute_in_value, _substitute_template

# Absolute ceiling we'll ever return in a single execute call, regardless of
# what an admin set on the connection / dataset. Protects the platform from a
# misconfigured 5-million-row dataset taking down the API process.
HARD_ROW_CAP = 500_000


class BindingMissingError(PermissionError):
    """Raised when (app_id, dataset_id) is not in app_dataset_bindings."""


class DatasetNotFoundError(LookupError):
    pass


async def execute(
    db: AsyncSession,
    *,
    app_id: str,
    dataset_id: str,
    params: dict[str, Any],
    calling_user_username: str,
    calling_user_id: str,
) -> DatasetPreviewResult:
    """Run a dataset on behalf of `app_id` for `calling_user`. Returns the same
    shape as preview (rows/columns/row_count/truncated/duration_ms).
    """
    # 1. Binding check — even org-visibility datasets must be explicitly bound
    #    to the calling app. This keeps the AI builder context deterministic
    #    and the blast radius tight.
    binding = (await db.execute(
        select(AppDatasetBinding).where(
            AppDatasetBinding.app_id == app_id,
            AppDatasetBinding.dataset_id == dataset_id,
        )
    )).scalar_one_or_none()
    if not binding:
        raise BindingMissingError(
            f"App '{app_id}' is not bound to dataset '{dataset_id}'"
        )

    # 2. Load dataset + connection
    ds = (await db.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
    if not ds:
        raise DatasetNotFoundError(f"Dataset '{dataset_id}' not found")
    conn = (await db.execute(select(Connection).where(Connection.id == ds.connection_id))).scalar_one_or_none()
    if not conn:
        raise DatasetNotFoundError(f"Connection '{ds.connection_id}' not found")

    # 3. Resolve limits
    row_limit = min(
        ds.row_limit_override or conn.default_row_limit or HARD_ROW_CAP,
        HARD_ROW_CAP,
    )
    timeout_s = ds.timeout_override or conn.default_timeout_seconds or 30

    # 4. Resolve credentials + dispatch
    credential = await connections_service.resolve_credential(db, conn.credential_secret_ref)

    # Build the param dict the executor receives. We *always* inject
    # :current_user so dataset authors can rely on it being present.
    effective_params = dict(params or {})
    effective_params["current_user"] = calling_user_username

    # Cache lookup (read-path only). Key includes current_user so PII scoping
    # never serves one user's rows to another.
    cache_ttl = getattr(ds, "cache_ttl_seconds", 0) or 0
    if cache_ttl > 0:
        from . import cache as _cache
        cached = _cache.get(dataset_id, params or {}, calling_user_username)
        if cached is not None:
            return cached

    start = time.time()
    truncated = False
    rows: list[dict] = []
    columns: list[tuple[str, str]] = []
    error_msg: str | None = None
    try:
        if ds.kind in ("table", "query"):
            sql = _resolve_sql(ds)
            rows, columns, truncated = await _execute_sql(
                conn, sql, effective_params, credential, row_limit, timeout_s
            )
        elif ds.kind == "api_call":
            rows, columns, truncated = await _execute_rest(
                conn, ds.definition, effective_params, credential, row_limit, timeout_s
            )
        else:
            raise ValueError(f"Unknown dataset kind '{ds.kind}'")

        # Apply PII column redaction. Columns tagged in the dataset's pii_tags
        # never come back over the runtime wire — only the placeholder string.
        # Future enhancement: a 'pii_view' permission that lets specific users
        # see real values; for now redaction is unconditional once tagged.
        from .pii import pii_columns, redact_rows
        pii_cols = pii_columns(ds.pii_tags)
        if pii_cols and rows:
            rows = redact_rows(rows, pii_cols)
    except Exception as e:
        error_msg = str(e)
        raise
    finally:
        elapsed_ms = int((time.time() - start) * 1000)
        # Audit log — note we record a *hash* of params, not the params
        # themselves, since they may contain user-supplied PII.
        param_hash = hashlib.sha256(
            json.dumps(params or {}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        details = (
            f"dataset.execute name='{ds.name}' app={app_id} "
            f"row_count={len(rows)} truncated={truncated} duration_ms={elapsed_ms} "
            f"param_hash={param_hash}"
        )
        if error_msg:
            details += f" error={error_msg[:120]}"
        db.add(AuditLog(
            user_id=calling_user_id,
            action="dataset.execute" if not error_msg else "dataset.execute.error",
            resource_type="dataset",
            resource_id=dataset_id,
            details=details,
        ))
        await db.commit()

    result = DatasetPreviewResult(
        rows=rows,
        columns=[DatasetPreviewColumn(name=c[0], type=c[1]) for c in columns],
        row_count=len(rows),
        truncated=truncated,
        duration_ms=elapsed_ms,
    )

    # Populate the cache on a successful read.
    if cache_ttl > 0 and not error_msg:
        from . import cache as _cache
        _cache.put(dataset_id, params or {}, calling_user_username, result, cache_ttl)

    return result


# --- SQL execution ---------------------------------------------------------


def _resolve_sql(ds: Dataset) -> str:
    if ds.kind == "query":
        sql = (ds.definition or {}).get("sql", "")
        if not sql:
            raise ValueError("dataset.definition.sql is required for query datasets")
        return sql
    # kind == "table"
    import re
    d = ds.definition or {}
    schema = d.get("schema")
    table = d.get("table_name")
    if not table:
        raise ValueError("dataset.definition.table_name is required for table datasets")
    for part in (schema or "", table):
        if part and not re.match(r"^[A-Za-z0-9_]+$", part):
            raise ValueError(f"Invalid identifier: '{part}'")
    cols = d.get("column_allowlist") or []
    for col in cols:
        if not re.match(r"^[A-Za-z0-9_]+$", col):
            raise ValueError(f"Invalid column identifier: '{col}'")
    select_list = ", ".join(cols) if cols else "*"
    full_table = f"{schema}.{table}" if schema and schema != "main" else table
    sql = f"SELECT {select_list} FROM {full_table}"
    where = d.get("where_template")
    if where:
        sql += f" WHERE {where}"
    return sql


async def _execute_sql(
    conn: Connection,
    sql: str,
    params: dict,
    password: str | None,
    row_limit: int,
    timeout_s: int,
) -> tuple[list[dict], list[tuple[str, str]], bool]:
    """Run the user's SQL and fetch at most `row_limit + 1` rows via the cursor.

    Intentionally does NOT wrap the user's query in `SELECT * FROM (...) LIMIT N`
    because LIMIT is not portable (MSSQL uses TOP/FETCH, Oracle uses FETCH FIRST,
    and MSSQL also disallows ORDER BY inside a derived table without TOP/OFFSET).
    Streaming + fetchmany gives us a dialect-agnostic row cap without rewriting
    the user's SQL.
    """
    dialect = sql_driver.get_dialect(conn.config.get("dialect", ""))
    sql_driver.ensure_driver(dialect)
    url = sql_driver.build_url(conn.config, password=password)
    engine = create_async_engine(url, pool_pre_ping=False)

    # asyncio.wait_for cancels the wrapping coroutine but cannot preempt a
    # blocking C-level driver call (pyodbc reads ahead synchronously). For
    # pyodbc/aioodbc the only way to actually cut off a slow query is to set
    # the underlying pyodbc.Connection.timeout (server-honored query timeout).
    # SQLAlchemy wraps aioodbc in AsyncAdapt_aioodbc_connection so the real
    # pyodbc connection lives at dbapi_conn.driver_connection._conn.
    # aiosqlite has no analogous attribute; the set is a best-effort no-op there.
    @event.listens_for(engine.sync_engine, "connect")
    def _set_query_timeout(dbapi_conn, _):
        for candidate in (
            # SQLAlchemy async adapter for aioodbc → pyodbc.Connection
            getattr(getattr(dbapi_conn, "driver_connection", None), "_conn", None),
            # Direct pyodbc / asyncpg connection (some drivers expose .timeout)
            dbapi_conn,
        ):
            if candidate is None:
                continue
            try:
                candidate.timeout = timeout_s
            except (AttributeError, TypeError):
                continue
            else:
                break

    try:
        used = _filter_unused_params(sql, params)

        async def _run() -> tuple[list[str], list[Any]]:
            async with engine.connect() as c:
                result = await c.execute(text(sql), used)
                col_names = list(result.keys())
                fetched = result.fetchmany(row_limit + 1)
                return col_names, fetched

        # Outer wait_for is a fallback safety net — for asyncpg and any driver
        # that actually honors asyncio cancellation it'll cut off cleanly.
        # Give a small grace window on top of the driver timeout in case the
        # driver-level timeout fires first.
        col_names, fetched = await asyncio.wait_for(_run(), timeout=timeout_s + 2)
        truncated = len(fetched) > row_limit
        kept = fetched[:row_limit]
        rows = [dict(zip(col_names, r)) for r in kept]
        rows = [{k: _coerce_for_json(v) for k, v in r.items()} for r in rows]
        columns = [(name, "string") for name in col_names]
        return rows, columns, truncated
    finally:
        await engine.dispose()


def _filter_unused_params(sql: str, params: dict) -> dict:
    """Keep only :name params that actually appear in the SQL.

    Naive substring match — fine for our use (sqlalchemy text() handles real
    parsing, but aiosqlite errors on truly unused params).
    """
    return {k: v for k, v in (params or {}).items() if f":{k}" in sql}


class MutationNotAllowedError(PermissionError):
    """Raised when a dataset mutation is attempted on a read-only connection or
    a dataset that has no mutation_sql defined."""


async def execute_mutation(
    db: AsyncSession,
    *,
    app_id: str,
    dataset_id: str,
    params: dict[str, Any],
    calling_user_username: str,
    calling_user_id: str,
) -> dict[str, Any]:
    """Run a write-back (INSERT/UPDATE/DELETE) defined on a dataset.

    Safety gates:
      - app must be bound to the dataset
      - the dataset's connection must NOT be read_only
      - the dataset definition must carry a `mutation_sql`
    Auto-injects :current_user. Audit-logged with a param hash (never raw params).
    """
    binding = (await db.execute(
        select(AppDatasetBinding).where(
            AppDatasetBinding.app_id == app_id,
            AppDatasetBinding.dataset_id == dataset_id,
        )
    )).scalar_one_or_none()
    if not binding:
        raise BindingMissingError(f"App '{app_id}' is not bound to dataset '{dataset_id}'")

    ds = (await db.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
    if not ds:
        raise DatasetNotFoundError(f"Dataset '{dataset_id}' not found")
    conn = (await db.execute(select(Connection).where(Connection.id == ds.connection_id))).scalar_one_or_none()
    if not conn:
        raise DatasetNotFoundError(f"Connection '{ds.connection_id}' not found")

    if conn.read_only:
        raise MutationNotAllowedError(
            f"Connection '{conn.name}' is read-only; mutations are not permitted"
        )

    mutation_sql = (ds.definition or {}).get("mutation_sql")
    if not mutation_sql:
        raise MutationNotAllowedError(
            "Dataset has no mutation_sql defined; cannot run a write-back"
        )

    timeout_s = ds.timeout_override or conn.default_timeout_seconds or 30
    credential = await connections_service.resolve_credential(db, conn.credential_secret_ref)

    effective_params = dict(params or {})
    effective_params["current_user"] = calling_user_username

    start = time.time()
    error_msg: str | None = None
    rows_affected = 0
    try:
        rows_affected = await _execute_mutation_sql(
            conn, mutation_sql, effective_params, credential, timeout_s
        )
        # A write invalidates cached read results (the data just changed).
        from . import cache as _cache
        _cache.invalidate_dataset(dataset_id)
    except Exception as e:
        error_msg = str(e)
        raise
    finally:
        elapsed_ms = int((time.time() - start) * 1000)
        param_hash = hashlib.sha256(
            json.dumps(params or {}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        details = (
            f"dataset.mutate name='{ds.name}' app={app_id} "
            f"rows_affected={rows_affected} duration_ms={elapsed_ms} param_hash={param_hash}"
        )
        if error_msg:
            details += f" error={error_msg[:120]}"
        db.add(AuditLog(
            user_id=calling_user_id,
            action="dataset.mutate" if not error_msg else "dataset.mutate.error",
            resource_type="dataset",
            resource_id=dataset_id,
            details=details,
        ))
        await db.commit()

    return {"rows_affected": rows_affected, "duration_ms": elapsed_ms}


async def _execute_mutation_sql(
    conn: Connection, sql: str, params: dict, password: str | None, timeout_s: int
) -> int:
    """Execute an INSERT/UPDATE/DELETE; return rows affected."""
    dialect = sql_driver.get_dialect(conn.config.get("dialect", ""))
    sql_driver.ensure_driver(dialect)
    url = sql_driver.build_url(conn.config, password=password)
    engine = create_async_engine(url, pool_pre_ping=False)
    try:
        used = _filter_unused_params(sql, params)

        async def _run() -> int:
            async with engine.begin() as c:
                result = await c.execute(text(sql), used)
                return result.rowcount or 0

        return await asyncio.wait_for(_run(), timeout=timeout_s + 2)
    finally:
        await engine.dispose()


# --- REST execution --------------------------------------------------------


async def _execute_rest(
    conn: Connection,
    definition: dict,
    params: dict,
    secret: str | None,
    row_limit: int,
    timeout_s: int,
) -> tuple[list[dict], list[tuple[str, str]], bool]:
    client = rest_driver.build_client(conn.config, secret=secret, timeout_seconds=timeout_s)
    try:
        method = (definition.get("method") or "GET").upper()
        path = _substitute_template(definition.get("path", ""), params)
        headers = {k: _substitute_template(v, params) for k, v in (definition.get("headers") or {}).items()}
        body = definition.get("body_template")
        json_body = _substitute_in_value(body, params) if body else None
        qparams = {
            k: _substitute_template(str(v), params)
            for k, v in (definition.get("query_params") or {}).items()
        }

        r = await client.request(method, path, headers=headers, params=qparams, json=json_body)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}

        if isinstance(data, list):
            truncated = len(data) > row_limit
            rows = data[:row_limit]
        else:
            rows = [data]
            truncated = False
        normalized = [r if isinstance(r, dict) else {"value": r} for r in rows]
        cols = list({k for row in normalized for k in row.keys()})
        return normalized, [(c, "string") for c in cols], truncated
    finally:
        await client.aclose()
