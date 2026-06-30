import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
from ..database import Base

# Secret categories that may be bound INTO apps via AppSetting.global_secret_ref.
# Resolved settings hand the DECRYPTED value to every user who can view the app,
# so platform credentials (ai_provider keys, agent tokens, SSH keys, database and
# SMTP passwords) must never be app-bindable — a developer could otherwise read
# them back in cleartext through /settings/resolved (privilege escalation).
APP_BINDABLE_SECRET_CATEGORIES = frozenset({"custom", "integration"})


class Secret(Base):
    __tablename__ = "secrets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(50), index=True)  # ai_provider, agent_token, ssh_private_key, database, smtp, integration, custom
    description: Mapped[str] = mapped_column(Text, default="")
    encrypted_value: Mapped[str] = mapped_column(Text, default="")  # Fernet-encrypted
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)  # Extra metadata (e.g., provider type, model)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(50))  # secret.read, secret.create, app.publish, etc.
    resource_type: Mapped[str] = mapped_column(String(50))  # secret, app, user
    resource_id: Mapped[str] = mapped_column(String(36))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
