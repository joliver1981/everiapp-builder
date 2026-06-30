from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..auth.dependencies import require_role
from ..auth.models import User
from .schemas import SecretCreate, SecretUpdate, SecretResponse
from .service import secrets_service

router = APIRouter()


def _to_response(secret) -> SecretResponse:
    return SecretResponse(
        id=secret.id,
        name=secret.name,
        category=secret.category,
        description=secret.description,
        is_set=bool(secret.encrypted_value),
        metadata_json=secret.metadata_json or {},
        created_at=secret.created_at.isoformat(),
        updated_at=secret.updated_at.isoformat(),
    )


@router.get("", response_model=list[SecretResponse])
async def list_secrets(
    category: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    secrets = await secrets_service.list_secrets(db, category)
    return [_to_response(s) for s in secrets]


# NOTE: declared before any /{secret_id} route so "pickable" isn't captured as an id.
@router.get("/pickable")
async def list_pickable_secrets(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """Minimal secret list for setup-wizard 'global_secret' fields.

    Restricted to APP-BINDABLE categories only: exposing platform-credential ids
    here would let developers bind them into an app and read the decrypted value
    back via /settings/resolved. The bind itself is also validated server-side
    (apps service) — this filter is the discovery layer of that defense.
    """
    from .models import APP_BINDABLE_SECRET_CATEGORIES
    secrets = await secrets_service.list_secrets(db)
    return [
        {"id": s.id, "name": s.name, "category": s.category, "is_set": bool(s.encrypted_value)}
        for s in secrets
        if s.category in APP_BINDABLE_SECRET_CATEGORIES
    ]


@router.post("", response_model=SecretResponse, status_code=status.HTTP_201_CREATED)
async def create_secret(
    body: SecretCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    existing = await secrets_service.get_secret_by_name(db, body.name)
    if existing:
        raise HTTPException(status_code=400, detail="Secret with this name already exists")
    secret = await secrets_service.create_secret(db, body, user.id)
    return _to_response(secret)


@router.get("/{secret_id}", response_model=SecretResponse)
async def get_secret(
    secret_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    secret = await secrets_service.get_secret(db, secret_id)
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")
    return _to_response(secret)


@router.put("/{secret_id}", response_model=SecretResponse)
async def update_secret(
    secret_id: str,
    body: SecretUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    secret = await secrets_service.update_secret(db, secret_id, body, user.id)
    if not secret:
        raise HTTPException(status_code=404, detail="Secret not found")
    return _to_response(secret)


@router.delete("/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    secret_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    deleted = await secrets_service.delete_secret(db, secret_id, user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Secret not found")
