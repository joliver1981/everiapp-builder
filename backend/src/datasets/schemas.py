from typing import Any, Literal

from pydantic import BaseModel, Field

DatasetKind = Literal["table", "query", "api_call"]
DatasetVisibility = Literal["private", "app_scoped", "org"]


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    connection_id: str
    kind: DatasetKind
    definition: dict = Field(default_factory=dict)
    parameter_schema: dict = Field(default_factory=dict)
    # Optional: caller can provide output_schema, otherwise we try to infer
    # one on save (SQL kinds only).
    output_schema: dict = Field(default_factory=dict)
    row_limit_override: int | None = None
    timeout_override: int | None = None
    visibility: DatasetVisibility = "private"
    # PII tagging: { column_name -> tag } where tag is a free-form label
    # ('email', 'phone', 'ssn', 'name', etc.). Tagged columns get their
    # values redacted in audit log details + preview row payloads for
    # non-owner viewers.
    pii_tags: dict[str, str] = Field(default_factory=dict)
    cache_ttl_seconds: int = 0


class DatasetUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    definition: dict | None = None
    parameter_schema: dict | None = None
    output_schema: dict | None = None
    row_limit_override: int | None = None
    timeout_override: int | None = None
    visibility: DatasetVisibility | None = None
    pii_tags: dict[str, str] | None = None
    cache_ttl_seconds: int | None = None


class DatasetResponse(BaseModel):
    id: str
    name: str
    description: str
    connection_id: str
    kind: DatasetKind
    definition: dict
    parameter_schema: dict
    output_schema: dict
    row_limit_override: int | None
    timeout_override: int | None
    visibility: DatasetVisibility
    owner_id: str
    pii_tags: dict[str, str] = Field(default_factory=dict)
    cache_ttl_seconds: int = 0
    created_at: str
    updated_at: str


# --- Preview ---------------------------------------------------------------


class DatasetPreviewRequest(BaseModel):
    """Run a dataset definition without saving. Used by the admin editor's
    'Run preview' button before the dataset has an id.
    """
    connection_id: str
    kind: DatasetKind
    definition: dict
    parameter_schema: dict = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)


class DatasetPreviewColumn(BaseModel):
    name: str
    type: str  # JSON Schema type: "string", "number", "integer", "boolean", "object", "array", "null"


class DatasetPreviewResult(BaseModel):
    rows: list[dict[str, Any]]
    columns: list[DatasetPreviewColumn]
    row_count: int
    truncated: bool
    duration_ms: int


# --- Introspection ---------------------------------------------------------


class IntrospectColumn(BaseModel):
    name: str
    type: str
    nullable: bool = True


class DatasetRecentCall(BaseModel):
    """A single audit entry for a dataset.execute or dataset.execute.error call."""
    action: str               # "dataset.execute" or "dataset.execute.error"
    user_id: str
    details: str
    created_at: str


class DatasetRecentCallsResult(BaseModel):
    calls: list[DatasetRecentCall]


class SchemaIntrospectionResult(BaseModel):
    """Tri-mode response — only one of these lists is meaningful per call.

    - schemas list when called with no query params
    - tables list when called with ?schema=
    - columns list when called with ?schema=&table=
    """
    schemas: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    columns: list[IntrospectColumn] = Field(default_factory=list)
