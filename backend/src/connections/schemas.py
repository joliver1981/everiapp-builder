from typing import Literal

from pydantic import BaseModel, Field

ConnectionKind = Literal["sql", "rest"]


class ConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    kind: ConnectionKind
    config: dict = Field(default_factory=dict)
    # Name (not id) of an entry in the secrets table that holds the credential
    # (e.g. SQL password, REST bearer token). Optional because some connections
    # are unauthenticated.
    credential_secret_ref: str | None = None
    default_row_limit: int = 500000
    default_timeout_seconds: int = 30
    read_only: bool = True


class ConnectionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    config: dict | None = None
    credential_secret_ref: str | None = None
    default_row_limit: int | None = None
    default_timeout_seconds: int | None = None
    read_only: bool | None = None


class ConnectionResponse(BaseModel):
    id: str
    name: str
    description: str
    kind: ConnectionKind
    config: dict
    credential_secret_ref: str | None
    default_row_limit: int
    default_timeout_seconds: int
    read_only: bool
    created_by: str
    created_at: str
    updated_at: str


class ConnectionTestResult(BaseModel):
    success: bool
    message: str
    response_time_ms: int | None = None
