"""HTTP routes for datasets.

Two router instances:
  - `router`             → mounted at /api/admin/datasets    (CRUD + preview)
  - `introspection_router` → mounted at /api/admin/connections
      (provides GET /{connection_id}/schema, which logically belongs with the
      datasets feature but URL-wise lives under connections)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_role
from ..auth.models import User
from ..connections.models import Connection
from ..connections.service import connections_service
from ..database import get_db
from . import introspect, runtime as runtime_exec
from .schemas import (
    DatasetCreate,
    DatasetPreviewRequest,
    DatasetPreviewResult,
    DatasetRecentCall,
    DatasetRecentCallsResult,
    DatasetResponse,
    DatasetUpdate,
    IntrospectColumn,
    SchemaIntrospectionResult,
)
from .service import datasets_service

router = APIRouter()
introspection_router = APIRouter()
runtime_router = APIRouter()
bindings_router = APIRouter()        # mounted at /api/apps
discoverable_router = APIRouter()    # mounted at /api/datasets


# --- CRUD ------------------------------------------------------------------


@router.get("", response_model=list[DatasetResponse])
async def list_datasets(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    return await datasets_service.list_datasets(db)


@router.post("", response_model=DatasetResponse, status_code=status.HTTP_201_CREATED)
async def create_dataset(
    body: DatasetCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        return await datasets_service.create_dataset(db, body, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{dataset_id}", response_model=DatasetResponse)
async def get_dataset(
    dataset_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    ds = await datasets_service.get_dataset(db, dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return ds


@router.put("/{dataset_id}", response_model=DatasetResponse)
async def update_dataset(
    dataset_id: str,
    body: DatasetUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    ds = await datasets_service.update_dataset(db, dataset_id, body, user.id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return ds


@router.delete("/{dataset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_dataset(
    dataset_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    deleted = await datasets_service.delete_dataset(db, dataset_id, user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Dataset not found")


# --- Observability ---------------------------------------------------------


@router.get("/{dataset_id}/recent-calls", response_model=DatasetRecentCallsResult)
async def dataset_recent_calls(
    dataset_id: str,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Return the most recent execute/error audit log entries for a dataset.

    Admin-only — these entries include user ids and apps that called the
    dataset, which is internal operational data.
    """
    rows = await datasets_service.recent_calls(db, dataset_id, limit=max(1, min(limit, 500)))
    return DatasetRecentCallsResult(calls=[DatasetRecentCall(**r) for r in rows])


@router.get("/{dataset_id}/lineage")
async def dataset_lineage(
    dataset_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Lineage: which connection feeds this dataset + which apps depend on it."""
    result = await datasets_service.lineage(db, dataset_id)
    if not result:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return result


# --- Preview ---------------------------------------------------------------


@router.post("/preview", response_model=DatasetPreviewResult)
async def preview_dataset(
    body: DatasetPreviewRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        return await datasets_service.preview(db, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Preview failed: {e}")


# --- Introspection ---------------------------------------------------------


@introspection_router.get("/{connection_id}/schema", response_model=SchemaIntrospectionResult)
async def introspect_schema(
    connection_id: str,
    schema: str | None = None,
    table: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Tri-mode endpoint:
       no params         → list schemas
       ?schema=X         → list tables in schema X
       ?schema=X&table=Y → list columns in table X.Y
    """
    result = await db.execute(select(Connection).where(Connection.id == connection_id))
    conn = result.scalar_one_or_none()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    if conn.kind != "sql":
        raise HTTPException(status_code=400, detail="Schema introspection is only supported for SQL connections")

    password = await connections_service.resolve_credential(db, conn.credential_secret_ref)
    try:
        if not schema:
            schemas = await introspect.list_schemas(conn, password)
            return SchemaIntrospectionResult(schemas=schemas)
        if schema and not table:
            tables = await introspect.list_tables(conn, schema, password)
            return SchemaIntrospectionResult(tables=tables)
        cols = await introspect.list_columns(conn, schema, table, password)
        return SchemaIntrospectionResult(
            columns=[IntrospectColumn(**c) for c in cols],
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Introspection failed: {e}")


# --- Runtime execute (called by deployed apps) ----------------------------


class _ExecuteRequest(BaseModel):
    params: dict = Field(default_factory=dict)


@runtime_router.post(
    "/{app_id}/datasets/{dataset_id}/execute",
    response_model=DatasetPreviewResult,
)
async def execute_dataset(
    app_id: str,
    dataset_id: str,
    body: _ExecuteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Run a dataset on behalf of the calling app.

    Auth: standard user JWT (the app passes through the user's token, injected
    by the runtime proxy as `window.__AIHUB_TOKEN__`).
    Binding: app_id must have an entry in `app_dataset_bindings` for dataset_id.
    """
    from ..rate_limit import dataset_limiter
    if not dataset_limiter.allow(app_id):
        raise HTTPException(status_code=429, detail="Dataset rate limit exceeded; slow down.")
    try:
        return await runtime_exec.execute(
            db,
            app_id=app_id,
            dataset_id=dataset_id,
            params=body.params,
            calling_user_username=user.username,
            calling_user_id=user.id,
        )
    except runtime_exec.BindingMissingError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except runtime_exec.DatasetNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Execution failed: {e}")


@runtime_router.post("/{app_id}/datasets/{dataset_id}/mutate")
async def mutate_dataset(
    app_id: str,
    dataset_id: str,
    body: _ExecuteRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Run a dataset's write-back (INSERT/UPDATE/DELETE) on behalf of an app.

    Requires: app bound to the dataset, the connection NOT read-only, and the
    dataset definition to carry a `mutation_sql`. Returns rows_affected.
    """
    from ..rate_limit import dataset_limiter
    if not dataset_limiter.allow(app_id):
        raise HTTPException(status_code=429, detail="Dataset rate limit exceeded; slow down.")
    try:
        return await runtime_exec.execute_mutation(
            db,
            app_id=app_id,
            dataset_id=dataset_id,
            params=body.params,
            calling_user_username=user.username,
            calling_user_id=user.id,
        )
    except runtime_exec.BindingMissingError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except runtime_exec.MutationNotAllowedError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except runtime_exec.DatasetNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Mutation failed: {e}")


# --- Bindings (used by App Builder) ----------------------------------------


@bindings_router.get("/{app_id}/datasets", response_model=list[DatasetResponse])
async def list_app_datasets(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List datasets currently bound to an app."""
    return await datasets_service.list_bindings(db, app_id)


@bindings_router.post(
    "/{app_id}/datasets/{dataset_id}",
    response_model=DatasetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def bind_app_dataset(
    app_id: str,
    dataset_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Idempotent: 201 whether newly created or already present."""
    try:
        await datasets_service.bind_dataset(db, app_id, dataset_id, user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    ds = await datasets_service.get_dataset(db, dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return ds


@bindings_router.delete(
    "/{app_id}/datasets/{dataset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unbind_app_dataset(
    app_id: str,
    dataset_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    removed = await datasets_service.unbind_dataset(db, app_id, dataset_id, user.id)
    if not removed:
        raise HTTPException(status_code=404, detail="Binding not found")


# --- Discoverable (for App Builder dataset picker) -------------------------


@discoverable_router.get("/discoverable", response_model=list[DatasetResponse])
async def list_discoverable(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Datasets the current user can attach to an app:
    anything visibility != private, plus their own private datasets.
    """
    return await datasets_service.list_discoverable(db, user.id)
