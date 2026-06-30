from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------- Deployment Targets ----------

class TargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    kind: Literal["agent", "ssh"]
    host: str = Field(..., min_length=1, max_length=200)
    port: int = Field(..., ge=1, le=65535)
    ssh_user: str | None = Field(default=None, max_length=100)
    port_range_start: int = Field(default=9100, ge=1, le=65535)
    port_range_end: int = Field(default=9199, ge=1, le=65535)
    environment: str = Field(default="dev", max_length=50)
    credential_secret_id: str | None = None
    extra_config: dict = Field(default_factory=dict)
    is_active: bool = True


class TargetUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    ssh_user: str | None = None
    port_range_start: int | None = None
    port_range_end: int | None = None
    environment: str | None = None
    credential_secret_id: str | None = None
    extra_config: dict | None = None
    is_active: bool | None = None


class TargetResponse(BaseModel):
    id: str
    name: str
    kind: str
    host: str
    port: int
    ssh_user: str | None
    port_range_start: int
    port_range_end: int
    environment: str
    credential_secret_id: str | None
    extra_config: dict
    is_active: bool
    last_seen_at: datetime | None
    last_seen_status: str | None
    agent_version: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TargetTestResponse(BaseModel):
    ok: bool
    detail: str = ""
    agent_version: str | None = None
    ports_used: list[int] = []
    ports_total: int | None = None


# ---------- Deployments ----------

class DeployRequest(BaseModel):
    target_id: str


class DeploymentResponse(BaseModel):
    id: str
    app_id: str
    version: int
    target_id: str
    allocated_port: int | None
    status: str
    public_url: str | None
    deployed_by: str
    started_at: datetime
    stopped_at: datetime | None
    last_health_at: datetime | None
    last_health_status: str | None
    error: str | None

    model_config = {"from_attributes": True}


class LogsResponse(BaseModel):
    lines: list[str]
