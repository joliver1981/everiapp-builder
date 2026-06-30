from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..auth.dependencies import require_role
from ..auth.models import User
from .schemas import AIProviderCreate, AIProviderUpdate, AIProviderResponse, AIProviderTestResult
from .service import ai_provider_service

router = APIRouter()


@router.get("", response_model=list[AIProviderResponse])
async def list_providers(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    return await ai_provider_service.list_providers(db)


@router.post("", response_model=AIProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider(
    body: AIProviderCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    return await ai_provider_service.create_provider(db, body, user.id)


@router.get("/{provider_id}", response_model=AIProviderResponse)
async def get_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    provider = await ai_provider_service.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider


@router.put("/{provider_id}", response_model=AIProviderResponse)
async def update_provider(
    provider_id: str,
    body: AIProviderUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    provider = await ai_provider_service.update_provider(db, provider_id, body, user.id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    deleted = await ai_provider_service.delete_provider(db, provider_id, user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Provider not found")


@router.post("/{provider_id}/test", response_model=AIProviderTestResult)
async def test_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    return await ai_provider_service.test_provider(db, provider_id, user.id)
