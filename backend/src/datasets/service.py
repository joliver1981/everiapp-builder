"""CRUD + preview executor for Dataset rows.

Mirrors `connections.service`:
  - `await db.flush()` between insert and audit-log write so the auto-gen id is
    available (CLAUDE.md rule).
  - Credentials resolved by name from the Secrets store via
    `connections_service.resolve_credential`.

The preview executor is the same code path the runtime proxy will use in PR4 —
we just cap rows at 101 and surface a `truncated` flag.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, time as _time, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ..connections.drivers import rest as rest_driver
from ..connections.drivers import sql as sql_driver
from ..connections.models import Connection
from ..connections.service import connections_service
from ..secrets.models import AuditLog
from .introspect import infer_output_schema_from_sample, infer_output_schema_sql
from .models import AppDatasetBinding, Dataset
from .schemas import (
    DatasetCreate,
    DatasetPreviewColumn,
    DatasetPreviewRequest,
    DatasetPreviewResult,
    DatasetResponse,
    DatasetUpdate,
)

PREVIEW_ROW_CAP = 100
# We fetch one extra row to detect truncation cheaply.
_PREVIEW_INTERNAL_FETCH = PREVIEW_ROW_CAP + 1


class DatasetsService:
    # --- CRUD --------------------------------------------------------------

    async def list_datasets(self, db: AsyncSession) -> list[DatasetResponse]:
        result = await db.execute(select(Dataset).order_by(Dataset.name))
        return [self._to_response(d) for d in result.scalars().all()]

    async def list_discoverable(self, db: AsyncSession, user_id: str) -> list[DatasetResponse]:
        """Datasets a non-admin developer can see for binding to their app:
        anything non-private, plus their own private datasets.
        """
        result = await db.execute(
            select(Dataset)
            .where(or_(Dataset.visibility != "private", Dataset.owner_id == user_id))
            .order_by(Dataset.name)
        )
        return [self._to_response(d) for d in result.scalars().all()]

    # --- Bindings ----------------------------------------------------------

    async def recent_calls(self, db: AsyncSession, dataset_id: str, limit: int = 50) -> list[dict]:
        """Fetch recent execute/error audit entries for a dataset."""
        result = await db.execute(
            select(AuditLog)
            .where(
                AuditLog.resource_id == dataset_id,
                AuditLog.action.in_(("dataset.execute", "dataset.execute.error")),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return [
            {
                "action": r.action,
                "user_id": r.user_id,
                "details": r.details or "",
                "created_at": r.created_at.isoformat(),
            }
            for r in result.scalars().all()
        ]

    async def list_bindings(self, db: AsyncSession, app_id: str) -> list[DatasetResponse]:
        """Datasets bound to a given app — the set the runtime proxy will allow
        and the AI builder context will inject."""
        result = await db.execute(
            select(Dataset)
            .join(AppDatasetBinding, AppDatasetBinding.dataset_id == Dataset.id)
            .where(AppDatasetBinding.app_id == app_id)
            .order_by(Dataset.name)
        )
        return [self._to_response(d) for d in result.scalars().all()]

    async def lineage(self, db: AsyncSession, dataset_id: str) -> dict:
        """Data lineage for one dataset: its connection + the apps bound to it.

        Answers 'what depends on this dataset?' (change-impact analysis) and
        'where does its data come from?' in one shot.
        """
        from ..apps.models import App
        from ..connections.models import Connection

        ds = await self._get_row(db, dataset_id)
        if not ds:
            return {}
        conn = (await db.execute(
            select(Connection).where(Connection.id == ds.connection_id)
        )).scalar_one_or_none()
        app_rows = (await db.execute(
            select(App.id, App.name)
            .join(AppDatasetBinding, AppDatasetBinding.app_id == App.id)
            .where(AppDatasetBinding.dataset_id == dataset_id)
        )).all()
        return {
            "dataset": {"id": ds.id, "name": ds.name, "kind": ds.kind},
            "connection": (
                {"id": conn.id, "name": conn.name, "kind": conn.kind,
                 "dialect": (conn.config or {}).get("dialect")}
                if conn else None
            ),
            "bound_apps": [{"id": r[0], "name": r[1]} for r in app_rows],
        }

    async def bind_dataset(self, db: AsyncSession, app_id: str, dataset_id: str, user_id: str) -> bool:
        """Idempotent: returns True if newly bound, False if already bound."""
        existing = (await db.execute(
            select(AppDatasetBinding).where(
                AppDatasetBinding.app_id == app_id,
                AppDatasetBinding.dataset_id == dataset_id,
            )
        )).scalar_one_or_none()
        if existing:
            return False
        # Confirm dataset exists before creating the binding
        ds = (await db.execute(select(Dataset).where(Dataset.id == dataset_id))).scalar_one_or_none()
        if not ds:
            raise ValueError(f"Dataset '{dataset_id}' not found")
        db.add(AppDatasetBinding(app_id=app_id, dataset_id=dataset_id))
        db.add(AuditLog(
            user_id=user_id,
            action="dataset.bind",
            resource_type="dataset",
            resource_id=dataset_id,
            details=f"Bound dataset '{ds.name}' to app {app_id}",
        ))
        await db.commit()
        return True

    async def unbind_dataset(self, db: AsyncSession, app_id: str, dataset_id: str, user_id: str) -> bool:
        existing = (await db.execute(
            select(AppDatasetBinding).where(
                AppDatasetBinding.app_id == app_id,
                AppDatasetBinding.dataset_id == dataset_id,
            )
        )).scalar_one_or_none()
        if not existing:
            return False
        await db.delete(existing)
        db.add(AuditLog(
            user_id=user_id,
            action="dataset.unbind",
            resource_type="dataset",
            resource_id=dataset_id,
            details=f"Unbound dataset {dataset_id} from app {app_id}",
        ))
        await db.commit()
        return True

    async def get_dataset(self, db: AsyncSession, dataset_id: str) -> DatasetResponse | None:
        d = await self._get_row(db, dataset_id)
        return self._to_response(d) if d else None

    async def _get_row(self, db: AsyncSession, dataset_id: str) -> Dataset | None:
        result = await db.execute(select(Dataset).where(Dataset.id == dataset_id))
        return result.scalar_one_or_none()

    async def _load_connection(self, db: AsyncSession, connection_id: str) -> Connection | None:
        result = await db.execute(select(Connection).where(Connection.id == connection_id))
        return result.scalar_one_or_none()

    async def create_dataset(
        self, db: AsyncSession, data: DatasetCreate, user_id: str
    ) -> DatasetResponse:
        conn = await self._load_connection(db, data.connection_id)
        if not conn:
            raise ValueError(f"Connection '{data.connection_id}' not found")

        output_schema = data.output_schema or {}
        if not output_schema and data.kind in ("query", "table"):
            output_schema = await self._best_effort_infer_output(db, conn, data.kind, data.definition, data.parameter_schema)

        ds = Dataset(
            name=data.name,
            description=data.description,
            connection_id=data.connection_id,
            kind=data.kind,
            definition=data.definition,
            parameter_schema=data.parameter_schema,
            output_schema=output_schema,
            row_limit_override=data.row_limit_override,
            timeout_override=data.timeout_override,
            visibility=data.visibility,
            pii_tags=data.pii_tags,
            cache_ttl_seconds=data.cache_ttl_seconds,
            owner_id=user_id,
        )
        db.add(ds)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            raise ValueError(f"A dataset named '{data.name}' already exists")

        db.add(AuditLog(
            user_id=user_id,
            action="dataset.create",
            resource_type="dataset",
            resource_id=ds.id,
            details=f"Created {data.kind} dataset '{data.name}'",
        ))
        await db.commit()
        await db.refresh(ds)
        return self._to_response(ds)

    async def update_dataset(
        self, db: AsyncSession, dataset_id: str, data: DatasetUpdate, user_id: str
    ) -> DatasetResponse | None:
        ds = await self._get_row(db, dataset_id)
        if not ds:
            return None

        if data.name is not None:
            ds.name = data.name
        if data.description is not None:
            ds.description = data.description
        if data.definition is not None:
            ds.definition = data.definition
        if data.parameter_schema is not None:
            ds.parameter_schema = data.parameter_schema
        if data.output_schema is not None:
            ds.output_schema = data.output_schema
        if data.row_limit_override is not None:
            ds.row_limit_override = data.row_limit_override
        if data.timeout_override is not None:
            ds.timeout_override = data.timeout_override
        if data.visibility is not None:
            ds.visibility = data.visibility
        if data.pii_tags is not None:
            ds.pii_tags = data.pii_tags
        if data.cache_ttl_seconds is not None:
            ds.cache_ttl_seconds = data.cache_ttl_seconds
        ds.updated_at = datetime.now(timezone.utc)

        db.add(AuditLog(
            user_id=user_id,
            action="dataset.update",
            resource_type="dataset",
            resource_id=ds.id,
            details=f"Updated dataset '{ds.name}'",
        ))
        await db.commit()
        await db.refresh(ds)
        return self._to_response(ds)

    async def delete_dataset(self, db: AsyncSession, dataset_id: str, user_id: str) -> bool:
        ds = await self._get_row(db, dataset_id)
        if not ds:
            return False

        # Cascade-delete bindings. SQLite doesn't enforce FKs by default, so
        # we have to clear these manually — otherwise the bindings table would
        # accumulate rows pointing at deleted datasets. Apps that were using
        # this dataset will get 403 on next call, which is the right behavior
        # (the dataset is gone; access is revoked).
        bindings = (await db.execute(
            select(AppDatasetBinding).where(AppDatasetBinding.dataset_id == dataset_id)
        )).scalars().all()
        binding_count = len(bindings)
        for b in bindings:
            await db.delete(b)

        details = f"Deleted dataset '{ds.name}'"
        if binding_count:
            details += f" and {binding_count} binding(s)"
        db.add(AuditLog(
            user_id=user_id,
            action="dataset.delete",
            resource_type="dataset",
            resource_id=ds.id,
            details=details,
        ))
        await db.delete(ds)
        await db.commit()
        return True

    # --- Preview -----------------------------------------------------------

    async def preview(
        self, db: AsyncSession, req: DatasetPreviewRequest
    ) -> DatasetPreviewResult:
        conn = await self._load_connection(db, req.connection_id)
        if not conn:
            raise ValueError(f"Connection '{req.connection_id}' not found")

        password = await connections_service.resolve_credential(db, conn.credential_secret_ref)

        start = time.time()
        if req.kind in ("table", "query"):
            sql, bind_params = self._build_sql(req)
            rows, columns, truncated = await self._execute_sql_preview(
                conn, sql, bind_params, password
            )
        elif req.kind == "api_call":
            rows, columns, truncated = await self._execute_rest_preview(
                conn, req.definition, req.params, password
            )
        else:
            raise ValueError(f"Unknown dataset kind '{req.kind}'")
        elapsed = int((time.time() - start) * 1000)

        return DatasetPreviewResult(
            rows=rows,
            columns=[DatasetPreviewColumn(name=c[0], type=c[1]) for c in columns],
            row_count=len(rows),
            truncated=truncated,
            duration_ms=elapsed,
        )

    def _build_sql(self, req: DatasetPreviewRequest) -> tuple[str, dict[str, Any]]:
        if req.kind == "query":
            sql = req.definition.get("sql", "")
            if not sql:
                raise ValueError("definition.sql is required for query datasets")
            return sql, dict(req.params or {})
        # kind == "table"
        d = req.definition
        schema = d.get("schema")
        table = d.get("table_name")
        if not table:
            raise ValueError("definition.table_name is required for table datasets")
        # Identifier safety: only allow alphanumerics + underscore.
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
        return sql, dict(req.params or {})

    async def _execute_sql_preview(
        self, conn: Connection, sql: str, params: dict, password: str | None
    ) -> tuple[list[dict], list[tuple[str, str]], bool]:
        """Run the user's SQL and pull at most PREVIEW_ROW_CAP+1 rows.

        We deliberately DO NOT wrap the user's SQL in a `LIMIT` subselect:
        - MSSQL has no LIMIT keyword (uses TOP / OFFSET FETCH).
        - MSSQL also forbids ORDER BY inside derived tables unless TOP/OFFSET
          is also present, so wrapping a user query with ORDER BY would fail.
        - Oracle uses FETCH FIRST n ROWS ONLY.
        Streaming + fetchmany works on every dialect and avoids buffering the
        full result set in memory.
        """
        dialect = sql_driver.get_dialect(conn.config.get("dialect", ""))
        sql_driver.ensure_driver(dialect)
        url = sql_driver.build_url(conn.config, password=password)
        engine = create_async_engine(url, pool_pre_ping=False)
        try:
            async with engine.connect() as c:
                used = _filter_unused_params(sql, params or {})
                result = await c.execute(text(sql), used)
                col_names = list(result.keys())
                fetched = result.fetchmany(_PREVIEW_INTERNAL_FETCH)
                truncated = len(fetched) > PREVIEW_ROW_CAP
                rows = [dict(zip(col_names, r)) for r in fetched[:PREVIEW_ROW_CAP]]
                rows = [{k: _coerce_for_json(v) for k, v in r.items()} for r in rows]
                columns = [(name, "string") for name in col_names]
                return rows, columns, truncated
        finally:
            await engine.dispose()

    async def _execute_rest_preview(
        self, conn: Connection, definition: dict, params: dict, secret: str | None
    ) -> tuple[list[dict], list[tuple[str, str]], bool]:
        client = rest_driver.build_client(
            conn.config,
            secret=secret,
            timeout_seconds=conn.default_timeout_seconds,
        )
        try:
            method = (definition.get("method") or "GET").upper()
            path = _substitute_template(definition.get("path", ""), params)
            headers = {k: _substitute_template(v, params) for k, v in (definition.get("headers") or {}).items()}
            body = definition.get("body_template")
            json_body = None
            if body:
                json_body = _substitute_in_value(body, params)
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
                rows = data[:PREVIEW_ROW_CAP]
                truncated = len(data) > PREVIEW_ROW_CAP
            else:
                rows = [data]
                truncated = False
            # Normalize non-dict entries into a {"value": ...} envelope so the
            # preview table has a column to render.
            normalized = [r if isinstance(r, dict) else {"value": r} for r in rows]
            cols = list({k for row in normalized for k in row.keys()})
            return normalized, [(c, "string") for c in cols], truncated
        finally:
            await client.aclose()

    # --- internal helpers --------------------------------------------------

    async def _best_effort_infer_output(
        self, db: AsyncSession, conn: Connection, kind: str, definition: dict, parameter_schema: dict
    ) -> dict:
        # Build a stub-params dict so :named placeholders in the SQL bind to NULL
        # at introspection time. Without this, a query like
        #   SELECT ... WHERE sale_date >= :since
        # fails with "bound parameter not provided" and we silently get {}.
        stub_params = {}
        if isinstance(parameter_schema, dict):
            for name in (parameter_schema.get("properties") or {}).keys():
                stub_params[name] = None
        if kind == "query":
            password = await connections_service.resolve_credential(db, conn.credential_secret_ref)
            return await infer_output_schema_sql(conn, definition.get("sql", ""), stub_params, password)
        if kind == "table":
            # Build the SELECT exactly like preview, then introspect via LIMIT 0.
            d = definition
            schema = d.get("schema")
            table = d.get("table_name")
            if not table:
                return {}
            for part in (schema or "", table):
                if part and not re.match(r"^[A-Za-z0-9_]+$", part):
                    return {}
            cols = d.get("column_allowlist") or []
            if any(not re.match(r"^[A-Za-z0-9_]+$", c) for c in cols):
                return {}
            select_list = ", ".join(cols) if cols else "*"
            full_table = f"{schema}.{table}" if schema and schema != "main" else table
            sql = f"SELECT {select_list} FROM {full_table}"
            password = await connections_service.resolve_credential(db, conn.credential_secret_ref)
            return await infer_output_schema_sql(conn, sql, {}, password)
        return {}

    def _to_response(self, ds: Dataset) -> DatasetResponse:
        return DatasetResponse(
            id=ds.id,
            name=ds.name,
            description=ds.description or "",
            connection_id=ds.connection_id,
            kind=ds.kind,
            definition=ds.definition or {},
            parameter_schema=ds.parameter_schema or {},
            output_schema=ds.output_schema or {},
            row_limit_override=ds.row_limit_override,
            timeout_override=ds.timeout_override,
            visibility=ds.visibility,
            owner_id=ds.owner_id,
            pii_tags=ds.pii_tags or {},
            cache_ttl_seconds=getattr(ds, "cache_ttl_seconds", 0) or 0,
            created_at=ds.created_at.isoformat(),
            updated_at=ds.updated_at.isoformat(),
        )


def _filter_unused_params(sql: str, params: dict) -> dict:
    """Keep only :name params that actually appear in the SQL.

    aiosqlite and some other drivers reject unused bound params. Naive
    substring match — fine for our use since text() handles real parsing.
    """
    return {k: v for k, v in (params or {}).items() if f":{k}" in sql}


def _coerce_for_json(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    # Numeric DB types (DECIMAL/NUMERIC -> Decimal) must serialize as JSON NUMBERS,
    # not strings — otherwise Recharts/aggregations in generated apps silently break
    # (chart with no bars, NaN totals). JSON has no decimal type and the frontend
    # treats these as JS numbers anyway, so float is the correct on-the-wire form.
    if isinstance(v, Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except Exception:
            return repr(v)
    # Dates/times -> ISO-8601 strings (stable, sortable, chart-friendly).
    if isinstance(v, (datetime, date, _time)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_coerce_for_json(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _coerce_for_json(val) for k, val in v.items()}
    return str(v)


def _substitute_template(template: str, params: dict) -> str:
    if not template:
        return template
    result = template
    for k, v in (params or {}).items():
        result = result.replace("{{" + str(k) + "}}", "" if v is None else str(v))
    return result


def _substitute_in_value(value: Any, params: dict) -> Any:
    if isinstance(value, str):
        return _substitute_template(value, params)
    if isinstance(value, dict):
        return {k: _substitute_in_value(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_in_value(x, params) for x in value]
    return value


datasets_service = DatasetsService()

# Expose for downstream modules / introspection-from-sample reuse.
__all__ = ["datasets_service", "infer_output_schema_from_sample", "PREVIEW_ROW_CAP"]
