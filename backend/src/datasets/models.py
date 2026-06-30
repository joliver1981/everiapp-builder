import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, PrimaryKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class Dataset(Base):
    """A named, parameterized query/call defined on top of a Connection.

    `kind` determines the shape of `definition`:
      - table:    { schema, table_name, column_allowlist[], where_template }
      - query:    { sql }  (with :named params)
      - api_call: { method, path, headers, body_template, query_params }

    `parameter_schema` and `output_schema` are JSON Schema documents. Output schema
    is introspected on save for SQL kinds (via LIMIT 0 + cursor description) and
    provided by the author for REST kinds.

    `visibility`:
      - private:    only the owner can bind/use
      - app_scoped: explicit per-app bindings only
      - org:        any app the org grants binding to

    Even for `org` visibility, an app must still appear in `app_dataset_bindings`
    to call the dataset at runtime — visibility gates discovery, bindings gate
    execution.
    """

    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    connection_id: Mapped[str] = mapped_column(String(36), ForeignKey("connections.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # "table" | "query" | "api_call"
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    parameter_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    output_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    row_limit_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    visibility: Mapped[str] = mapped_column(String(20), default="private", index=True)
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    # { column_name -> pii_tag } — e.g. {"email": "email", "ssn": "ssn"}
    pii_tags: Mapped[dict] = mapped_column(JSON, default=dict)
    # TTL for query-result caching. 0 = no cache (default). Only read executes
    # are cached; mutations invalidate the whole dataset cache.
    cache_ttl_seconds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class AppDatasetBinding(Base):
    """Explicit grant: app X is allowed to call dataset Y via the runtime proxy.

    Even for org-visibility datasets, runtime execution requires a row here. This
    keeps the AI builder's available-datasets context deterministic and keeps the
    blast radius tight — revoking access is a single DELETE.
    """

    __tablename__ = "app_dataset_bindings"

    app_id: Mapped[str] = mapped_column(String(36), ForeignKey("apps.id"))
    dataset_id: Mapped[str] = mapped_column(String(36), ForeignKey("datasets.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (PrimaryKeyConstraint("app_id", "dataset_id"),)
