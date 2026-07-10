import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, PrimaryKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class Connection(Base):
    """A reusable, credentialed pointer to an external system (SQL DB or REST API).

    Credentials are never stored here directly — `credential_secret_ref` points at
    a row in the `secrets` table, which holds the Fernet-encrypted value.

    `config` holds kind-specific connection params:
      - sql:  { dialect, host, port, database, extra_params }
      - rest: { base_url, auth_type, default_headers }
      - ai:   rest keys (+ default_query) plus { provider, models, default_model,
              chat_path, models_path } — see connections/providers.py
    """

    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), index=True)  # "sql" | "rest" | "ai"
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    credential_secret_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    default_row_limit: Mapped[int] = mapped_column(Integer, default=500000)
    default_timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    # When True (REST connections only), bound apps may make free-form HTTP calls
    # THROUGH this connection via callConnection() — the app picks the method/
    # path/body, the platform injects base_url + credentials server-side. Default
    # off so a connection isn't app-callable until an admin opts in.
    app_callable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AppConnectionBinding(Base):
    """Explicit grant: app X may make free-form calls through connection Y.

    Mirrors AppDatasetBinding — the connection must also be `app_callable`, and
    the app must have a row here, before callConnection() will run. Revoking is a
    single DELETE, and the AI builder's available-connections context is exactly
    the set of these bindings.
    """

    __tablename__ = "app_connection_bindings"

    app_id: Mapped[str] = mapped_column(String(36), ForeignKey("apps.id"))
    connection_id: Mapped[str] = mapped_column(String(36), ForeignKey("connections.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (PrimaryKeyConstraint("app_id", "connection_id"),)
