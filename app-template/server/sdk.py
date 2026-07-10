"""Server-function SDK types — PLATFORM-OWNED, do not edit.

A server function is a Python file at server/functions/<name>.py:

    from sdk import Ctx

    CONFIG = {"timeout_s": 30}          # optional; literal only, max 120

    def handler(args, ctx: Ctx):
        rows = ctx.db.query("SELECT * FROM orders", limit=50_000)["rows"]
        return {"count": len(rows)}

The app's UI invokes it with `callFunction('<name>', args)` from
@aihub/app-sdk. `args` is whatever JSON the UI sent; the return value must be
JSON-serializable (convert DataFrames with .to_dict('records'), numpy values
with .item()/list()) and at most 5 MiB.

Functions run ON the platform in their own interpreter, synchronously, with a
hard timeout — no background work, no state between invocations. Available
imports: the Python standard library, the platform's curated libraries
(pandas, numpy, openpyxl, reportlab, pypdf, dateutil), and other files under
server/. External HTTP must go through ctx.call_connection — the same
admin-configured, attached Connections the browser SDK uses; credentials are
injected server-side and never visible to your code.

This module defines TYPES for editor/AI reference; the real `ctx` object is
supplied by the platform at run time and matches these signatures exactly.
"""
from __future__ import annotations

from typing import Any, Protocol, TypedDict


class QueryResult(TypedDict):
    """ctx.db.query result."""
    rows: list[dict[str, Any]]
    columns: list[str]
    row_count: int
    #: True when the row cap cut the result — raise `limit=` if you need more.
    truncated: bool


class ExecResult(TypedDict):
    """ctx.db.exec result."""
    rows_affected: int
    last_insert_rowid: int


class ConnectionCallResult(TypedDict):
    """ctx.call_connection result."""
    #: The upstream HTTP status code — check `>= 400` before using body.
    status: int
    headers: dict[str, str]
    #: Parsed JSON when the response is JSON, otherwise the raw text.
    body: Any
    #: True if the response exceeded the platform's cap and was cut.
    truncated: bool


class AiChatResult(TypedDict):
    """ctx.ai_chat result — resolves (never raises) on provider errors."""
    #: The provider's HTTP status — check `>= 400` before using text.
    status: int
    #: The assistant's reply text ('' when the provider errored).
    text: str
    model: str
    #: The full, untranslated provider response body.
    raw: Any
    #: Human-readable provider error when status >= 400, else None.
    error: str | None


class DecisionResult(TypedDict):
    """ctx.ai_decide result — resolves to the decision's fallback on LLM trouble."""
    value: Any
    #: 'llm' | 'cache' | 'fallback' — where the value came from.
    source: str
    latency_ms: int


class Db(Protocol):
    """The app's own SQLite store — the same data useAppDB sees in the UI."""

    def query(self, sql: str, params: dict[str, Any] | None = None,
              scope: str | None = None, limit: int | None = None) -> QueryResult:
        """Run a SELECT. Named params bind as :name; :current_user is always
        available. Default row cap 50k — pass limit= (max 500k) for more."""
        ...

    def exec(self, sql: str, params: dict[str, Any] | None = None) -> ExecResult:
        """Run an INSERT/UPDATE/DELETE."""
        ...


class Ctx(Protocol):
    """Platform capabilities available to a server function.

    Same permission boundary as the app's browser code: Connections must be
    admin-configured, marked app-callable, and attached to this app.
    """

    #: The app this function belongs to.
    app_id: str
    #: The invoking user: {"id": str, "username": str}.
    user: dict[str, str]
    #: The app's own database.
    db: Db

    def log(self, *args: Any) -> None:
        """Log a line — returned to the app in the invoke response's logs[]."""
        ...

    def call_connection(self, id_or_name: str, method: str = "GET", path: str = "/",
                        query: dict[str, Any] | None = None,
                        headers: dict[str, Any] | None = None,
                        body: Any = None) -> ConnectionCallResult:
        """One outbound HTTP call through an attached Connection. `path` is
        RELATIVE to the connection's base URL; auth is injected server-side."""
        ...

    def ai_chat(self, id_or_name: str, messages: Any, model: str | None = None,
                system: str | None = None, max_tokens: int | None = None,
                temperature: float | None = None,
                extra: dict[str, Any] | None = None) -> AiChatResult:
        """One chat completion through an attached AI-provider Connection.
        `messages` is a list of {role, content} or a plain user string."""
        ...

    def ai_decide(self, name: str, input: dict[str, Any] | None = None) -> DecisionResult:
        """Invoke one of the app's registered AI decisions (decisions.json)."""
        ...
