from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from .schemas import (
    DeployRequest,
    DeploymentResponse,
    LogsResponse,
    TargetCreate,
    TargetResponse,
    TargetTestResponse,
    TargetUpdate,
)
from .service import deployments_service

# Admin endpoints — manage deployment targets
admin_router = APIRouter()


@admin_router.get("/deployment-targets", response_model=list[TargetResponse])
async def list_targets(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    return await deployments_service.list_targets(db)


@admin_router.post("/deployment-targets", response_model=TargetResponse)
async def create_target(
    data: TargetCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        return await deployments_service.create_target(db, data, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@admin_router.put("/deployment-targets/{target_id}", response_model=TargetResponse)
async def update_target(
    target_id: str,
    data: TargetUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    target = await deployments_service.update_target(db, target_id, data, user.id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    return target


@admin_router.delete("/deployment-targets/{target_id}")
async def delete_target(
    target_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin")),
):
    try:
        ok = await deployments_service.delete_target(db, target_id, user.id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="Target not found")
    return {"deleted": True}


@admin_router.post("/deployment-targets/{target_id}/test", response_model=TargetTestResponse)
async def test_target(
    target_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    info = await deployments_service.test_target(db, target_id)
    return TargetTestResponse(
        ok=info.ok,
        detail=info.detail,
        agent_version=info.agent_version,
        ports_used=info.ports_used or [],
        ports_total=info.ports_total,
    )


# Deployment endpoints — per app
deployments_router = APIRouter()


@deployments_router.get(
    "/apps/{app_id}/deployments", response_model=list[DeploymentResponse]
)
async def list_deployments(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer", "user")),
):
    return await deployments_service.list_deployments(db, app_id)


@deployments_router.post(
    "/apps/{app_id}/versions/{version}/deploy", response_model=DeploymentResponse
)
async def deploy_version(
    app_id: str,
    version: int,
    request: DeployRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    try:
        return await deployments_service.deploy(db, app_id, version, request.target_id, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@deployments_router.post("/apps/{app_id}/versions/{version}/deploy-blue-green")
async def deploy_blue_green(
    app_id: str,
    version: int,
    request: DeployRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """Zero-downtime deploy: bring up the new version alongside the current one,
    health-check it, then cut over (or abort, leaving the current version live)."""
    try:
        return await deployments_service.blue_green_deploy(
            db, app_id, version, request.target_id, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@deployments_router.post("/deployments/{deployment_id}/stop", response_model=DeploymentResponse)
async def stop_deployment(
    deployment_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    deployment = await deployments_service.stop(db, deployment_id, user.id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return deployment


@deployments_router.post("/deployments/{deployment_id}/redeploy", response_model=DeploymentResponse)
async def redeploy(
    deployment_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    existing = await deployments_service.get_deployment(db, deployment_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if existing.status in ("pending", "building", "uploading", "running"):
        await deployments_service.stop(db, deployment_id, user.id)
    try:
        return await deployments_service.deploy(
            db, existing.app_id, existing.version, existing.target_id, user.id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@deployments_router.get("/deployments/{deployment_id}/logs", response_model=LogsResponse)
async def get_logs(
    deployment_id: str,
    n: int = 200,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    lines = await deployments_service.get_logs(db, deployment_id, n)
    return LogsResponse(lines=lines)


@deployments_router.get("/deployments/{deployment_id}/health")
async def get_health(
    deployment_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    result = await deployments_service.health_check(db, deployment_id)
    return {"ok": result.ok, "detail": result.detail}
