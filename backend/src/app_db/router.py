"""HTTP routes for the per-app SQLite store.

Mounted at /api/apps so the routes are:
    POST   /api/apps/{app_id}/db/query        — read
    POST   /api/apps/{app_id}/db/exec         — write (insert/update/delete)
    GET    /api/apps/{app_id}/db/tables       — admin browser
    POST   /api/apps/{app_id}/db/migrate      — apply a versioned migration batch
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_role, require_role_allow_scoped
from ..auth.models import User
from ..database import get_db
from ..secrets.models import AuditLog
from . import service as appdb

router = APIRouter()


class _QueryIn(BaseModel):
    sql: str
    params: dict[str, Any] = Field(default_factory=dict)
    scope: str = "all"     # 'all' | 'user'


class _ExecIn(BaseModel):
    sql: str
    params: dict[str, Any] = Field(default_factory=dict)


class _MigrateIn(BaseModel):
    migrations: list[dict[str, Any]]  # [{version: int, name: str, sql: str}, ...]


def _hash_params(params: dict[str, Any]) -> str:
    import hashlib
    import json
    return hashlib.sha256(
        json.dumps(params, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


@router.post("/{app_id}/db/query")
async def query(
    app_id: str,
    body: _QueryIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from ..rate_limit import app_db_limiter
    if not app_db_limiter.allow(app_id):
        raise HTTPException(status_code=429, detail="App DB rate limit exceeded; slow down.")
    try:
        result = appdb.execute_query(
            app_id=app_id,
            sql=body.sql,
            params=body.params,
            current_user=user.username,
            scope=body.scope,
        )
    except Exception as e:
        # Audit the failure too
        db.add(AuditLog(
            user_id=user.id,
            action="app_db.query.error",
            resource_type="app_db",
            resource_id=app_id,
            details=f"sql_hash={_hash_params({'sql': body.sql})} error={str(e)[:140]}",
        ))
        await db.commit()
        raise HTTPException(status_code=400, detail=f"Query failed: {e}")

    db.add(AuditLog(
        user_id=user.id,
        action="app_db.query",
        resource_type="app_db",
        resource_id=app_id,
        details=(
            f"rows={result.row_count} truncated={result.truncated} "
            f"param_hash={_hash_params(body.params)}"
        ),
    ))
    await db.commit()
    return {
        "rows": result.rows,
        "columns": result.columns,
        "row_count": result.row_count,
        "truncated": result.truncated,
    }


@router.post("/{app_id}/db/exec")
async def exec_(
    app_id: str,
    body: _ExecIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from ..rate_limit import app_db_limiter
    if not app_db_limiter.allow(app_id):
        raise HTTPException(status_code=429, detail="App DB rate limit exceeded; slow down.")
    try:
        result = appdb.execute_exec(
            app_id=app_id,
            sql=body.sql,
            params=body.params,
            current_user=user.username,
        )
    except Exception as e:
        db.add(AuditLog(
            user_id=user.id,
            action="app_db.exec.error",
            resource_type="app_db",
            resource_id=app_id,
            details=f"sql_hash={_hash_params({'sql': body.sql})} error={str(e)[:140]}",
        ))
        await db.commit()
        raise HTTPException(status_code=400, detail=f"Exec failed: {e}")

    db.add(AuditLog(
        user_id=user.id,
        action="app_db.exec",
        resource_type="app_db",
        resource_id=app_id,
        details=(
            f"affected={result.rows_affected} "
            f"last_insert_rowid={result.last_insert_rowid} "
            f"param_hash={_hash_params(body.params)}"
        ),
    ))
    await db.commit()
    return {
        "rows_affected": result.rows_affected,
        "last_insert_rowid": result.last_insert_rowid,
    }


@router.post("/{app_id}/db/migrate")
async def migrate(
    app_id: str,
    body: _MigrateIn,
    # allow_scoped: the SDK's useAppSchema runs this from INSIDE a previewed
    # app with the injected (purpose=preview, app-scoped) token. Plain
    # require_role would reject it and every preview boot would fail with
    # "Database failed to initialize". The app_id scope check still confines
    # the token to this app's own database.
    user: User = Depends(require_role_allow_scoped("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Apply a versioned set of migrations. Admin-only because schema changes
    affect every user of the app."""
    items = []
    for m in body.migrations:
        items.append((int(m["version"]), str(m["name"]), str(m["sql"])))
    result = appdb.apply_migrations(app_id, items)

    db.add(AuditLog(
        user_id=user.id,
        action="app_db.migrate",
        resource_type="app_db",
        resource_id=app_id,
        details=f"applied={result.get('applied_versions')} refused={len(result.get('refused', []))}",
    ))
    await db.commit()
    return result


@router.get("/{app_id}/db/tables")
async def tables(
    app_id: str,
    user: User = Depends(require_role("admin")),
):
    """Admin-only table browser."""
    info = appdb.list_tables(app_id)
    return {
        "tables": [
            {
                "name": t.name,
                "row_count": t.row_count,
                "columns": t.columns,
            }
            for t in info
        ],
        "db_size_bytes": appdb.db_size_bytes(app_id),
    }
