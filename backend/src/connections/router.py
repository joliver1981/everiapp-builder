from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from .providers import AI_PROVIDERS
from .schemas import (
    ConnectionCreate,
    ConnectionResponse,
    ConnectionTestResult,
    ConnectionUpdate,
    FetchModelsRequest,
    FetchModelsResult,
)
from .service import connections_service

router = APIRouter()


@router.get("", response_model=list[ConnectionResponse])
async def list_connections(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    return await connections_service.list_connections(db)


@router.post("", response_model=ConnectionResponse, status_code=status.HTTP_201_CREATED)
async def create_connection(
    body: ConnectionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        return await connections_service.create_connection(db, body, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# NOTE: declared before GET /{connection_id} so "pickable" isn't captured as an id.
@router.get("/pickable")
async def list_pickable_connections(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """Minimal connection list for setup-wizard 'connection' fields.

    Developers installing an app need to PICK a connection but must not see
    its config/credentials — so this returns identity fields only (the full
    ConnectionResponse stays admin-only).
    """
    conns = await connections_service.list_connections(db)
    return [
        {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "kind": c.kind,
            "dialect": (c.config or {}).get("dialect", ""),
        }
        for c in conns
    ]


# NOTE: like /pickable, declared before GET /{connection_id} so the literal
# path isn't captured as a connection id.
@router.get("/ai-providers")
async def list_ai_providers(
    user: User = Depends(require_role("admin")),
):
    """The AI-provider preset registry: known base URLs, auth conventions,
    models/chat endpoints, and suggested models. Drives the create form's
    provider picker so an admin never has to look up a base URL."""
    return {
        "providers": [
            {"provider": key, **preset} for key, preset in AI_PROVIDERS.items()
        ]
    }


@router.post("/fetch-models", response_model=FetchModelsResult)
async def fetch_models(
    body: FetchModelsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Pull the live model list from an AI provider using form-state config —
    works before the connection is saved, so the create dialog can offer it."""
    try:
        models = await connections_service.fetch_provider_models(
            db,
            config=body.config,
            credential_secret_ref=body.credential_secret_ref,
            timeout_seconds=body.timeout_seconds,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return FetchModelsResult(models=models)


@router.get("/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    conn = await connections_service.get_connection(db, connection_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


@router.put("/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: str,
    body: ConnectionUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        conn = await connections_service.update_connection(db, connection_id, body, user.id)
    except ValueError as e:
        # AI-kind config replacements are validated eagerly.
        raise HTTPException(status_code=400, detail=str(e))
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        deleted = await connections_service.delete_connection(db, connection_id, user.id)
    except ValueError as e:
        # Datasets still depend on this connection — block instead of orphan.
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Connection not found")


# Developers may test a connection they're about to bind in a setup wizard —
# the result is only ok/message/latency, never credentials.
@router.post("/{connection_id}/test", response_model=ConnectionTestResult)
async def test_connection(
    connection_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    return await connections_service.test_connection(db, connection_id, user.id)


@router.get("/health/all")
async def health_all(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    """Ping every connection and return a health badge per connection.

    Drives the admin health dashboard. SQL connections run SELECT 1; REST
    connections do a HEAD. Returns {connections: [{id, name, ok, message, ms}]}.
    """
    conns = await connections_service.list_connections(db)
    out = []
    for c in conns:
        result = await connections_service.test_connection(db, c.id, user.id)
        out.append({
            "id": c.id,
            "name": c.name,
            "kind": c.kind,
            "ok": result.success,
            "message": result.message,
            "response_time_ms": result.response_time_ms,
        })
    return {"connections": out}
