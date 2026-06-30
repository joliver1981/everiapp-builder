"""Export an app as a portable zip package / import one back in.

Routes mount under the /api/apps prefix. POST /import can't be shadowed by
another router: no earlier /api/apps router defines a POST on a single
/{segment} path (the apps router only POSTs to "").
"""
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_role
from ..auth.models import User
from ..database import get_db
from .service import PackageError, packaging_service

router = APIRouter()


@router.get("/{app_id}/export")
async def export_app(
    app_id: str,
    version: int | None = Query(None, ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    try:
        zip_bytes, filename = await packaging_service.export_app(db, app_id, version)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PackageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import", status_code=201)
async def import_app(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    data = await file.read()
    try:
        app = await packaging_service.import_app(db, data, user.id)
    except PackageError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "app_id": app.id,
        "name": app.name,
        "message": f"Imported '{app.name}' as a new draft app",
    }
