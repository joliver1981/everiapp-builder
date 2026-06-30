import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class DeploymentTarget(Base):
    __tablename__ = "deployment_targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True)
    kind: Mapped[str] = mapped_column(String(20))  # agent | ssh
    host: Mapped[str] = mapped_column(String(200))
    port: Mapped[int] = mapped_column(Integer, default=8765)  # agent HTTPS port OR ssh port
    ssh_user: Mapped[str | None] = mapped_column(String(100), nullable=True)
    port_range_start: Mapped[int] = mapped_column(Integer, default=9100)
    port_range_end: Mapped[int] = mapped_column(Integer, default=9199)
    environment: Mapped[str] = mapped_column(String(50), default="dev")  # free-text label
    credential_secret_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("secrets.id"), nullable=True)
    extra_config: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # ok | error
    agent_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_id: Mapped[str] = mapped_column(String(36), ForeignKey("apps.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    target_id: Mapped[str] = mapped_column(String(36), ForeignKey("deployment_targets.id"), index=True)
    allocated_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending | building | uploading | running | stopped | failed
    public_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    deployed_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_health_status: Mapped[str | None] = mapped_column(String(20), nullable=True)  # ok | error
    # Consecutive failed health probes; reset to 0 on a healthy probe. Drives
    # auto-rollback once it crosses the configured threshold.
    consecutive_health_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    build_artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)


# Statuses considered to still hold a port (for collision detection).
ACTIVE_DEPLOYMENT_STATUSES = ("pending", "building", "uploading", "running")
