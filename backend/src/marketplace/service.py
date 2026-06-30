"""Marketplace service — listing CRUD and app install logic."""
import shutil
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..apps.models import App
from ..apps.schemas import AppSettingCreate
from ..apps.service import apps_service
from .models import MarketplaceListing
from .schemas import ListingCreate, ListingUpdate


class MarketplaceService:
    async def list_listings(self, db: AsyncSession, category: str | None = None) -> list[MarketplaceListing]:
        query = select(MarketplaceListing).where(MarketplaceListing.is_active == True)
        if category:
            query = query.where(MarketplaceListing.category == category)
        query = query.order_by(MarketplaceListing.install_count.desc())
        result = await db.execute(query)
        return list(result.scalars().all())

    async def get_listing(self, db: AsyncSession, listing_id: str) -> MarketplaceListing | None:
        result = await db.execute(
            select(MarketplaceListing).where(MarketplaceListing.id == listing_id)
        )
        return result.scalar_one_or_none()

    async def create_listing(
        self, db: AsyncSession, data: ListingCreate, user_id: str, user_name: str
    ) -> MarketplaceListing:
        # Get the app
        app = await apps_service.get_app(db, data.app_id)
        if not app:
            raise ValueError("App not found")
        if app.status != "published":
            raise ValueError("App must be published before listing on marketplace")

        # Check for existing listing
        result = await db.execute(
            select(MarketplaceListing).where(MarketplaceListing.app_id == data.app_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            raise ValueError("App is already listed on the marketplace")

        listing = MarketplaceListing(
            app_id=app.id,
            name=app.name,
            description=data.description or app.description,
            icon=app.icon,
            category=data.category,
            tags=data.tags,
            version=app.current_version,
            published_by=user_id,
            publisher_name=user_name,
            setup_wizard=app.setup_wizard,
        )
        db.add(listing)
        await db.commit()
        await db.refresh(listing)
        return listing

    async def update_listing(
        self, db: AsyncSession, listing_id: str, data: ListingUpdate
    ) -> MarketplaceListing | None:
        listing = await self.get_listing(db, listing_id)
        if not listing:
            return None

        if data.description is not None:
            listing.description = data.description
        if data.category is not None:
            listing.category = data.category
        if data.tags is not None:
            listing.tags = data.tags
        if data.is_active is not None:
            listing.is_active = data.is_active
        listing.updated_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(listing)
        return listing

    async def delete_listing(self, db: AsyncSession, listing_id: str) -> bool:
        listing = await self.get_listing(db, listing_id)
        if not listing:
            return False
        await db.delete(listing)
        await db.commit()
        return True

    async def install_app(
        self, db: AsyncSession, listing_id: str, user_id: str, wizard_values: dict
    ) -> App:
        """Install a marketplace app for a user — copies files and creates settings."""
        listing = await self.get_listing(db, listing_id)
        if not listing:
            raise ValueError("Listing not found")

        # Copy version files to a new app
        source_dir = Path(settings.app_data_dir) / listing.app_id / "versions" / f"v{listing.version}"
        if not source_dir.exists():
            raise ValueError("Source app files not found")

        # Create new app
        from ..apps.schemas import AppCreate
        new_app = await apps_service.create_app(
            db, AppCreate(name=listing.name, description=listing.description, icon=listing.icon), user_id
        )

        # Copy files from source version to new app's draft
        target_dir = Path(settings.app_data_dir) / new_app.id / "draft" / "frontend"
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir)

        # Mark as installed from marketplace
        new_app.installed_from = listing_id
        new_app.setup_wizard = listing.setup_wizard

        # Create app settings from wizard values (secrets encrypted, upserted
        # through the shared wizard-apply path)
        if wizard_values and listing.setup_wizard:
            await apps_service.apply_wizard_values(db, new_app, wizard_values)

        # Increment install count
        listing.install_count += 1

        await db.commit()
        await db.refresh(new_app)
        return new_app


marketplace_service = MarketplaceService()
