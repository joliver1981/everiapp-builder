import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, Text, Boolean, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..database import Base


class App(Base):
    __tablename__ = "apps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(50), default="app-window")
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft, published, archived
    current_version: Mapped[int] = mapped_column(Integer, default=0)
    ai_toggle_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    bug_widget_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    bug_fix_auto_approve_max_risk: Mapped[str] = mapped_column(String(20), default="none")  # none | low | medium
    # AI self-verify after every chat turn.
    # Level: off | tsc | tsc_build | tsc_build_boot | tsc_build_boot_runtime
    ai_verify_level: Mapped[str] = mapped_column(String(30), default="tsc_build_boot_runtime")
    ai_verify_max_iterations: Mapped[int] = mapped_column(Integer, default=8)
    setup_wizard: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # wizard schema JSON
    installed_from: Mapped[str | None] = mapped_column(String(36), nullable=True)  # marketplace listing ID
    # Iframe embedding: allow this app to be framed by external portals.
    # server_default keeps raw INSERTs (test seeders, migrations) valid on a
    # freshly create_all'd table, matching the ALTER-TABLE migration defaults.
    embed_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    embed_allowed_origins: Mapped[str] = mapped_column(Text, default="", server_default="")  # CSV of scheme://host[:port]
    # Slug on the external marketplace after first publish (re-publish targets it)
    marketplace_slug: Mapped[str] = mapped_column(String(100), default="", server_default="")
    # Last semver published to the marketplace — seeds the publish dialog's bump
    # buttons and the downgrade guard. Empty until the first external publish.
    last_published_version: Mapped[str] = mapped_column(String(20), default="", server_default="")
    # Publisher-authored setup instructions (markdown) — shown on the marketplace
    # listing and after install (e.g. "ask IT for a read-only ERP account").
    setup_instructions: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    creator: Mapped["User"] = relationship(back_populates="apps", foreign_keys=[created_by])
    versions: Mapped[list["AppVersion"]] = relationship(back_populates="app", order_by="AppVersion.version.desc()", cascade="all, delete-orphan")
    permissions: Mapped[list["AppPermission"]] = relationship(back_populates="app", cascade="all, delete-orphan")
    settings: Mapped[list["AppSetting"]] = relationship(back_populates="app", cascade="all, delete-orphan")
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="app", cascade="all, delete-orphan")


class AppVersion(Base):
    __tablename__ = "app_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_id: Mapped[str] = mapped_column(String(36), ForeignKey("apps.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text, default="")
    published_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"))
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)  # file checksums, metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    app: Mapped["App"] = relationship(back_populates="versions")


class AppPermission(Base):
    __tablename__ = "app_permissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_id: Mapped[str] = mapped_column(String(36), ForeignKey("apps.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(200), nullable=True)  # AD group
    permission: Mapped[str] = mapped_column(String(20), default="access")  # access, edit
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    app: Mapped["App"] = relationship(back_populates="permissions")


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_id: Mapped[str] = mapped_column(String(36), ForeignKey("apps.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    label: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(20))  # string, secret, number, boolean, select, url
    description: Mapped[str] = mapped_column(Text, default="")
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    default_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)  # encrypted if type=secret
    global_secret_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)  # FK to secrets.id
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    app: Mapped["App"] = relationship(back_populates="settings")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    app_id: Mapped[str] = mapped_column(String(36), ForeignKey("apps.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    app: Mapped["App"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", order_by="Message.created_at", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user, assistant, system
    content: Mapped[str] = mapped_column(Text)
    files_changed: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # [{path, action, content}]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


# Import User to resolve relationship
from ..auth.models import User  # noqa: E402, F401
