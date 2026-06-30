from pydantic import BaseModel


class SecretCreate(BaseModel):
    name: str
    category: str  # ai_provider, database, smtp, integration, custom
    description: str = ""
    value: str = ""
    metadata_json: dict = {}


class SecretUpdate(BaseModel):
    description: str | None = None
    value: str | None = None
    metadata_json: dict | None = None


class SecretResponse(BaseModel):
    id: str
    name: str
    category: str
    description: str
    is_set: bool  # Whether a value has been set (never expose the actual value)
    metadata_json: dict
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True
