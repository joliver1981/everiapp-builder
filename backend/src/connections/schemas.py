from typing import Literal

from pydantic import BaseModel, Field

ConnectionKind = Literal["sql", "rest", "ai"]


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
    # REST only: allow bound apps to make free-form calls through this connection.
    app_callable: bool = False


class ConnectionUpdate(BaseModel):
    # kind is immutable after create. Accepted here ONLY so a client echoing it
    # back gets an explicit 400 on a mismatch instead of a silent no-op that
    # leaves the row's kind and its replaced config disagreeing.
    kind: ConnectionKind | None = None
    name: str | None = None
    description: str | None = None
    config: dict | None = None
    credential_secret_ref: str | None = None
    default_row_limit: int | None = None
    default_timeout_seconds: int | None = None
    read_only: bool | None = None
    app_callable: bool | None = None


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
    app_callable: bool
    created_by: str
    created_at: str
    updated_at: str


class ConnectionTestResult(BaseModel):
    success: bool
    message: str
    response_time_ms: int | None = None


class FetchModelsRequest(BaseModel):
    """Ask a provider for its live model list using form-state config — works
    before the connection row exists, so the create dialog can offer it."""

    config: dict = Field(default_factory=dict)
    credential_secret_ref: str | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=120)


class FetchModelsResult(BaseModel):
    models: list[str]
