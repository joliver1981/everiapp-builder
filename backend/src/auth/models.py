import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ..database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(20), default="user")  # admin, developer, user
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set only for LOCAL accounts (username+password). Empty for SSO/AD/mock
    # users. Format: pbkdf2_sha256$iters$salt$hash (see auth/passwords.py).
    password_hash: Mapped[str] = mapped_column(String(255), default="", server_default="")
    ad_groups: Mapped[str] = mapped_column(Text, default="[]")  # JSON array of AD group names
    # This developer's personal "skills" — standing preferences injected into
    # every generation turn they run (org-wide standards live in the
    # custom_system_prompt platform setting, Admin → Platform).
    dev_standards: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Which IdP authenticated this user ('mock'/'local'/'ldap') + the stable
    # external id from that IdP. Lets repeat logins update instead of duplicate.
    auth_provider: Mapped[str] = mapped_column(String(20), default="mock")
    external_id: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    apps: Mapped[list["App"]] = relationship(back_populates="creator", foreign_keys="App.created_by")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class IdentityProviderConfig(Base):
    """Stored config for an external identity provider (LDAP/AD today).

    The auth chain loads enabled providers ordered by (is_default DESC, id ASC)
    and tries each before falling back to mock/local auth.

    `auth_provider` + `external_id` on the User row tracks which IdP minted a
    user so subsequent logins update (not duplicate) the record.
    """
    __tablename__ = "identity_provider_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider_type: Mapped[str] = mapped_column(String(20))   # "ldap"
    provider_name: Mapped[str] = mapped_column(String(100))  # friendly label
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    group_role_mapping: Mapped[str] = mapped_column(Text, default="{}")  # JSON: {group: role}
    default_role: Mapped[str] = mapped_column(String(20), default="user")
    auto_provision: Mapped[bool] = mapped_column(Boolean, default=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# Import App here to avoid circular imports at module level
from ..apps.models import App  # noqa: E402, F401
