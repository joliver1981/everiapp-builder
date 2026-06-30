from pydantic import BaseModel


class PublishRequest(BaseModel):
    notes: str = ""
    # When the security scan blocks a publish, an admin (only) may set this to
    # publish anyway. The override is recorded in the audit log.
    override_security: bool = False


class VersionResponse(BaseModel):
    id: str
    app_id: str
    version: int
    notes: str
    published_by: str
    manifest: dict
    created_at: str

    class Config:
        from_attributes = True
