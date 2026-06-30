from pydantic import BaseModel


class ListingCreate(BaseModel):
    app_id: str
    description: str = ""
    category: str = "general"
    tags: list[str] = []


class ListingUpdate(BaseModel):
    description: str | None = None
    category: str | None = None
    tags: list[str] | None = None
    is_active: bool | None = None


class ListingResponse(BaseModel):
    id: str
    app_id: str
    name: str
    description: str
    icon: str
    category: str
    tags: list[str]
    version: int
    published_by: str
    publisher_name: str
    install_count: int
    is_active: bool
    setup_wizard: dict | None = None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class InstallRequest(BaseModel):
    listing_id: str
    wizard_values: dict = {}


class ExternalPublishRequest(BaseModel):
    """Publish an app to the external AIHub Marketplace website.

    Credentials default to the platform settings (marketplace_url /
    marketplace_api_key) — per-request values are an override for testing
    against a different marketplace.
    """
    app_id: str
    category: str = "general"
    tags: list[str] = []
    short_description: str = ""
    # Full markdown listing description; persisted onto the app when provided.
    description: str | None = None
    license: str = "MIT"
    release_notes: str = ""
    # Markdown setup instructions shown on the listing; persisted on the App.
    setup_instructions: str | None = None
    # Which saved SNAPSHOT to publish; None = latest (current behavior).
    version: int | None = None
    # Public RELEASE semver (e.g. "1.4.0"); None = legacy "{snapshot}.0.0".
    version_semver: str | None = None
    capture_screenshots: bool = True
    marketplace_url: str | None = None
    marketplace_api_key: str | None = None


class SuggestMetadataRequest(BaseModel):
    """Ask the AI to draft marketplace listing metadata for an app."""
    app_id: str
    version: int | None = None  # ground release notes in the diff up to this version


class RemoteInstallRequest(BaseModel):
    """Install an app from the external marketplace."""
    slug: str
    version: str | None = None
    wizard_values: dict = {}
