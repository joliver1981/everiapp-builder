"""Marketplace router — browse, list, and install apps."""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_role
from ..auth.models import User
from ..database import get_db
from ..platform_settings.service import get_setting
from . import external
from .schemas import (
    ListingCreate, ListingUpdate, ListingResponse, InstallRequest,
    ExternalPublishRequest, RemoteInstallRequest, SuggestMetadataRequest,
)
from .service import marketplace_service

router = APIRouter()


def _listing_to_response(listing) -> ListingResponse:
    return ListingResponse(
        id=listing.id,
        app_id=listing.app_id,
        name=listing.name,
        description=listing.description,
        icon=listing.icon,
        category=listing.category,
        tags=listing.tags or [],
        version=listing.version,
        published_by=listing.published_by,
        publisher_name=listing.publisher_name,
        install_count=listing.install_count,
        is_active=listing.is_active,
        setup_wizard=listing.setup_wizard,
        created_at=listing.created_at.isoformat(),
        updated_at=listing.updated_at.isoformat(),
    )


@router.get("", response_model=list[ListingResponse])
async def list_listings(
    category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    listings = await marketplace_service.list_listings(db, category)
    return [_listing_to_response(l) for l in listings]


# NOTE: declared before GET /{listing_id} so "remote" isn't captured as an id.
@router.get("/remote")
async def browse_remote_marketplace(
    q: str = Query(""),
    category: str = Query(""),
    sort: str = Query("popular"),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Proxy the external marketplace's public app search."""
    try:
        return await external.browse_remote(db, q=q, category=category, sort=sort, page=page)
    except external.MarketplaceError as e:
        raise HTTPException(status_code=400, detail=str(e))


# NOTE: declared before GET /{listing_id} so "published-versions" isn't captured as an id.
@router.get("/published-versions")
async def published_versions(
    app_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """Semvers already published for this app's listing — the publish dialog
    greys these out so you can't collide (best-effort; empty on any failure)."""
    try:
        return await external.remote_published_versions(db, app_id)
    except external.MarketplaceError as e:
        raise HTTPException(status_code=400, detail=str(e))


# NOTE: declared before GET /{listing_id} so "publish-config" isn't captured as an id.
@router.get("/publish-config")
async def publish_config(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """Whether external-marketplace publishing is configured, so the builder's
    Publish dialog can warn upfront and link the dev to setup. Returns presence
    flags only — never the secret API key value."""
    url = (await get_setting(db, "marketplace_url") or "").rstrip("/")
    key = await get_setting(db, "marketplace_api_key") or ""
    return {
        "marketplace_url": url,
        "url_configured": bool(url),
        "key_configured": bool(key),
        "configured": bool(url and key),
    }


@router.get("/{listing_id}", response_model=ListingResponse)
async def get_listing(
    listing_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    listing = await marketplace_service.get_listing(db, listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    return _listing_to_response(listing)


@router.post("", response_model=ListingResponse, status_code=status.HTTP_201_CREATED)
async def create_listing(
    body: ListingCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    try:
        listing = await marketplace_service.create_listing(db, body, user.id, user.display_name)
        return _listing_to_response(listing)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{listing_id}", response_model=ListingResponse)
async def update_listing(
    listing_id: str,
    body: ListingUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    listing = await marketplace_service.update_listing(db, listing_id, body)
    if not listing:
        raise HTTPException(status_code=404, detail="Listing not found")
    return _listing_to_response(listing)


@router.delete("/{listing_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_listing(
    listing_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    deleted = await marketplace_service.delete_listing(db, listing_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Listing not found")


@router.post("/install")
async def install_app(
    body: InstallRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        app = await marketplace_service.install_app(db, body.listing_id, user.id, body.wizard_values)
        return {
            "app_id": app.id,
            "name": app.name,
            "message": f"Successfully installed '{app.name}'",
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/publish-external")
async def publish_to_external_marketplace(
    body: ExternalPublishRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """Full publish pipeline to the external AIHub Marketplace: package zip,
    screenshots, direct-to-storage uploads, then the listing itself."""
    try:
        return await external.publish_app(
            db, body.app_id, user,
            category=body.category,
            tags=body.tags,
            short_description=body.short_description,
            description=body.description,
            license=body.license,
            release_notes=body.release_notes,
            setup_instructions=body.setup_instructions,
            version=body.version,
            version_semver=body.version_semver,
            capture_shots=body.capture_screenshots,
            marketplace_url=body.marketplace_url,
            marketplace_api_key=body.marketplace_api_key,
        )
    except external.MarketplaceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/suggest-metadata")
async def suggest_listing_metadata(
    body: SuggestMetadataRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """AI-draft the marketplace listing fields for the publish dialog."""
    from .suggest import suggest_metadata
    try:
        return await suggest_metadata(db, body.app_id, user, version=body.version)
    except external.MarketplaceError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---- Remote gallery (install from the external marketplace; browse is
# declared above the /{listing_id} wildcard) ----

@router.post("/remote/install")
async def install_remote_app(
    body: RemoteInstallRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """Download an app from the external marketplace and import it locally."""
    try:
        app = await external.install_remote(
            db, user.id, slug=body.slug, version=body.version,
            wizard_values=body.wizard_values,
        )
    except external.MarketplaceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "app_id": app.id,
        "name": app.name,
        "message": f"Installed '{app.name}' from the marketplace",
    }
