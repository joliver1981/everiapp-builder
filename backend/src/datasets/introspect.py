"""Schema introspection + output-shape inference.

For SQL connections: list schemas/tables/columns via INFORMATION_SCHEMA (or
PRAGMA on sqlite, all_tab_columns on oracle). For datasets, infer the output
JSON Schema by wrapping the user's SQL in a LIMIT-0 subselect and reading
cursor.description.

For REST: `infer_output_schema_from_sample` walks a sample response payload
and produces a JSON Schema.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ..connections.drivers import sql as sql_driver
from ..connections.models import Connection


# --- SQL schema/table/column listing ---------------------------------------


async def list_schemas(connection: Connection, password: str | None) -> list[str]:
    dialect = sql_driver.get_dialect(connection.config.get("dialect", ""))
    sql_driver.ensure_driver(dialect)
    url = sql_driver.build_url(connection.config, password=password)
    engine = create_async_engine(url, pool_pre_ping=False)
    try:
        async with engine.connect() as c:
            if dialect.name == "sqlite":
                return ["main"]
            if dialect.name in ("postgres", "mysql", "mssql"):
                rows = (await c.execute(text(
                    "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name"
                ))).all()
                return [r[0] for r in rows]
            if dialect.name == "oracle":
                rows = (await c.execute(text(
                    "SELECT DISTINCT owner FROM all_tables ORDER BY owner"
                ))).all()
                return [r[0] for r in rows]
    finally:
        await engine.dispose()
    return []


async def list_tables(connection: Connection, schema: str, password: str | None) -> list[str]:
    dialect = sql_driver.get_dialect(connection.config.get("dialect", ""))
    sql_driver.ensure_driver(dialect)
    url = sql_driver.build_url(connection.config, password=password)
    engine = create_async_engine(url, pool_pre_ping=False)
    try:
        async with engine.connect() as c:
            if dialect.name == "sqlite":
                rows = (await c.execute(text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                ))).all()
                return [r[0] for r in rows]
            if dialect.name in ("postgres", "mysql", "mssql"):
                rows = (await c.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :schema ORDER BY table_name"
                ), {"schema": schema})).all()
                return [r[0] for r in rows]
            if dialect.name == "oracle":
                rows = (await c.execute(text(
                    "SELECT table_name FROM all_tables WHERE owner = :owner ORDER BY table_name"
                ), {"owner": schema})).all()
                return [r[0] for r in rows]
    finally:
        await engine.dispose()
    return []


async def list_columns(
    connection: Connection, schema: str, table: str, password: str | None
) -> list[dict[str, Any]]:
    dialect = sql_driver.get_dialect(connection.config.get("dialect", ""))
    sql_driver.ensure_driver(dialect)
    url = sql_driver.build_url(connection.config, password=password)
    engine = create_async_engine(url, pool_pre_ping=False)
    try:
        async with engine.connect() as c:
            if dialect.name == "sqlite":
                # PRAGMA can't take bound params; identifier must be quoted safely.
                # We only allow identifier characters here.
                safe = "".join(ch for ch in table if ch.isalnum() or ch in ("_",))
                rows = (await c.execute(text(f"PRAGMA table_info({safe})"))).all()
                # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
                return [
                    {"name": r[1], "type": _sql_type_to_json_schema(r[2] or ""), "nullable": not bool(r[3])}
                    for r in rows
                ]
            if dialect.name in ("postgres", "mysql", "mssql"):
                rows = (await c.execute(text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = :table "
                    "ORDER BY ordinal_position"
                ), {"schema": schema, "table": table})).all()
                return [
                    {"name": r[0], "type": _sql_type_to_json_schema(r[1] or ""),
                     "nullable": (r[2] == "YES" if isinstance(r[2], str) else bool(r[2]))}
                    for r in rows
                ]
            if dialect.name == "oracle":
                rows = (await c.execute(text(
                    "SELECT column_name, data_type, nullable FROM all_tab_columns "
                    "WHERE owner = :owner AND table_name = :table ORDER BY column_id"
                ), {"owner": schema, "table": table})).all()
                return [
                    {"name": r[0], "type": _sql_type_to_json_schema(r[1] or ""),
                     "nullable": (r[2] == "Y")}
                    for r in rows
                ]
    finally:
        await engine.dispose()
    return []


# --- Output schema inference -----------------------------------------------


async def infer_output_schema_sql(
    connection: Connection, sql: str, params: dict, password: str | None
) -> dict:
    """Execute the user's SQL with NULL-substituted params, read column metadata,
    fetch zero rows. Returns {} on any failure — callers treat this as best-effort.

    We previously wrapped as `SELECT * FROM (sql) _shape LIMIT 0` but that
    broke on MSSQL (no LIMIT keyword + ORDER BY in derived table) and Oracle.
    Streaming + fetchmany(0) is dialect-agnostic.
    """
    try:
        dialect = sql_driver.get_dialect(connection.config.get("dialect", ""))
        sql_driver.ensure_driver(dialect)
        url = sql_driver.build_url(connection.config, password=password)
    except Exception:
        return {}

    engine = create_async_engine(url, pool_pre_ping=False)
    try:
        # NULL out bound params so :named placeholders don't blow up at parse time.
        substituted_params = {k: None for k in (params or {}).keys()}
        # Only filter to params actually present in the SQL (some drivers reject unused).
        substituted_params = {k: v for k, v in substituted_params.items() if f":{k}" in sql}
        async with engine.connect() as c:
            result = await c.execute(text(sql), substituted_params)
            columns = list(result.keys())
            # Don't drain rows — close immediately. result.keys() is populated
            # from cursor.description as soon as execute() returns.
            result.close()
            return {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {col: {"type": "string"} for col in columns},
                },
            }
    except Exception:
        return {}
    finally:
        await engine.dispose()


def infer_output_schema_from_sample(value: Any) -> dict:
    """Walk a sample REST response and produce a JSON Schema."""
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {k: infer_output_schema_from_sample(v) for k, v in value.items()},
        }
    if isinstance(value, list):
        if not value:
            return {"type": "array", "items": {}}
        return {"type": "array", "items": infer_output_schema_from_sample(value[0])}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if value is None:
        return {"type": "null"}
    return {"type": "string"}


# --- helpers ----------------------------------------------------------------


def _sql_type_to_json_schema(sql_type: str) -> str:
    """Crude SQL type → JSON Schema type mapping. Good enough for the column-
    listing UI and inferred output schemas — not used for runtime validation.
    """
    t = (sql_type or "").upper()
    if any(s in t for s in ("INT", "SERIAL", "BIGSERIAL")):
        return "integer"
    if any(s in t for s in ("REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "NUMBER")):
        return "number"
    if "BOOL" in t:
        return "boolean"
    if any(s in t for s in ("JSON", "JSONB")):
        return "object"
    return "string"
