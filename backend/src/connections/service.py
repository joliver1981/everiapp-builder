"""CRUD + test-connection for Connection rows.

Mirrors the structure of `ai_providers.service`:
  - Audit log entries written for every mutation, with a `flush()` between the
    insert and the audit row so the auto-generated id is available (see
    CLAUDE.md note about default=lambda firing at flush time).
  - Credentials are read from the existing Secrets store by name — never
    embedded in the Connection row.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ..secrets.encryption import encryption_service
from ..secrets.models import AuditLog, Secret
from . import providers
from .drivers import rest as rest_driver
from .drivers import sql as sql_driver
from .drivers.sql import DriverNotInstalledError
from .models import Connection
from .schemas import ConnectionCreate, ConnectionResponse, ConnectionTestResult, ConnectionUpdate


class ConnectionsService:
    async def list_connections(self, db: AsyncSession) -> list[ConnectionResponse]:
        result = await db.execute(select(Connection).order_by(Connection.name))
        return [self._to_response(c) for c in result.scalars().all()]

    async def get_connection(self, db: AsyncSession, connection_id: str) -> Optional[ConnectionResponse]:
        result = await db.execute(select(Connection).where(Connection.id == connection_id))
        conn = result.scalar_one_or_none()
        return self._to_response(conn) if conn else None

    async def _get_row(self, db: AsyncSession, connection_id: str) -> Optional[Connection]:
        result = await db.execute(select(Connection).where(Connection.id == connection_id))
        return result.scalar_one_or_none()

    async def create_connection(
        self, db: AsyncSession, data: ConnectionCreate, user_id: str
    ) -> ConnectionResponse:
        if data.kind == "ai":
            providers.validate_ai_config(data.config)
        conn = Connection(
            name=data.name,
            description=data.description,
            kind=data.kind,
            config=data.config,
            credential_secret_ref=data.credential_secret_ref,
            default_row_limit=data.default_row_limit,
            default_timeout_seconds=data.default_timeout_seconds,
            read_only=data.read_only,
            app_callable=data.app_callable,
            created_by=user_id,
        )
        db.add(conn)
        try:
            await db.flush()  # populate conn.id before AuditLog references it
        except IntegrityError:
            await db.rollback()
            raise ValueError(f"A connection named '{data.name}' already exists")

        db.add(AuditLog(
            user_id=user_id,
            action="connection.create",
            resource_type="connection",
            resource_id=conn.id,
            details=f"Created {data.kind} connection '{data.name}'",
        ))
        await db.commit()
        await db.refresh(conn)
        return self._to_response(conn)

    async def update_connection(
        self, db: AsyncSession, connection_id: str, data: ConnectionUpdate, user_id: str
    ) -> Optional[ConnectionResponse]:
        conn = await self._get_row(db, connection_id)
        if not conn:
            return None

        if data.kind is not None and data.kind != conn.kind:
            raise ValueError(
                f"A connection's kind cannot be changed after creation "
                f"(this one is '{conn.kind}') — create a new connection instead"
            )
        if data.name is not None:
            conn.name = data.name
        if data.description is not None:
            conn.description = data.description
        if data.config is not None:
            if conn.kind == "ai":
                providers.validate_ai_config(data.config)
            conn.config = data.config
        if data.credential_secret_ref is not None:
            conn.credential_secret_ref = data.credential_secret_ref or None
        if data.default_row_limit is not None:
            conn.default_row_limit = data.default_row_limit
        if data.default_timeout_seconds is not None:
            conn.default_timeout_seconds = data.default_timeout_seconds
        if data.read_only is not None:
            conn.read_only = data.read_only
        if data.app_callable is not None:
            conn.app_callable = data.app_callable
        conn.updated_at = datetime.now(timezone.utc)

        db.add(AuditLog(
            user_id=user_id,
            action="connection.update",
            resource_type="connection",
            resource_id=conn.id,
            details=f"Updated connection '{conn.name}'",
        ))
        await db.commit()
        await db.refresh(conn)
        return self._to_response(conn)

    async def delete_connection(self, db: AsyncSession, connection_id: str, user_id: str) -> bool:
        conn = await self._get_row(db, connection_id)
        if not conn:
            return False

        # Block deletion if any datasets reference this connection. SQLite
        # doesn't enforce FKs by default, so we have to check ourselves —
        # otherwise the dataset rows would silently dangle.
        # Lazy import to avoid a circular dep at module load.
        from ..datasets.models import Dataset
        existing = (await db.execute(
            select(Dataset).where(Dataset.connection_id == connection_id)
        )).scalars().all()
        if existing:
            names = ", ".join(d.name for d in existing[:5])
            more = "" if len(existing) <= 5 else f" (+{len(existing) - 5} more)"
            raise ValueError(
                f"Cannot delete connection '{conn.name}': "
                f"{len(existing)} dataset(s) still reference it: {names}{more}. "
                "Delete the datasets first."
            )

        # Clear app→connection bindings first (their FK to connections has no
        # cascade, so with foreign_keys=ON the DELETE would otherwise fail for
        # any connection an app is bound to). Revoking access on delete is fine.
        from sqlalchemy import delete as _delete
        from .models import AppConnectionBinding
        await db.execute(_delete(AppConnectionBinding).where(
            AppConnectionBinding.connection_id == connection_id))

        db.add(AuditLog(
            user_id=user_id,
            action="connection.delete",
            resource_type="connection",
            resource_id=conn.id,
            details=f"Deleted connection '{conn.name}'",
        ))
        await db.delete(conn)
        await db.commit()
        return True

    async def test_connection(
        self, db: AsyncSession, connection_id: str, user_id: str
    ) -> ConnectionTestResult:
        conn = await self._get_row(db, connection_id)
        if not conn:
            return ConnectionTestResult(success=False, message="Connection not found")

        credential = await self.resolve_credential(db, conn.credential_secret_ref)

        start = time.time()
        try:
            if conn.kind == "sql":
                await self._test_sql(conn, credential)
            elif conn.kind == "rest":
                await self._test_rest(conn, credential)
            elif conn.kind == "ai":
                await self._test_ai(conn, credential)
            else:
                return ConnectionTestResult(success=False, message=f"Unknown kind '{conn.kind}'")
        except DriverNotInstalledError as e:
            return ConnectionTestResult(success=False, message=str(e))
        except Exception as e:
            return ConnectionTestResult(success=False, message=str(e))
        finally:
            db.add(AuditLog(
                user_id=user_id,
                action="connection.test",
                resource_type="connection",
                resource_id=conn.id,
                details=f"Tested connection '{conn.name}'",
            ))
            await db.commit()

        elapsed = int((time.time() - start) * 1000)
        return ConnectionTestResult(success=True, message="Connection successful", response_time_ms=elapsed)

    async def resolve_credential(self, db: AsyncSession, ref: str | None) -> str | None:
        if not ref:
            return None
        result = await db.execute(select(Secret).where(Secret.name == ref))
        secret = result.scalar_one_or_none()
        if not secret or not secret.encrypted_value:
            return None
        return encryption_service.decrypt(secret.encrypted_value)

    async def _test_sql(self, conn: Connection, password: str | None) -> None:
        dialect = sql_driver.get_dialect(conn.config.get("dialect", ""))
        sql_driver.ensure_driver(dialect)
        url = sql_driver.build_url(conn.config, password=password)
        engine = create_async_engine(url, pool_pre_ping=False)
        try:
            async with engine.connect() as c:
                from sqlalchemy import text as _text
                await c.execute(_text("SELECT 1"))
        finally:
            await engine.dispose()

    async def _test_rest(self, conn: Connection, secret: str | None) -> None:
        client = rest_driver.build_client(
            conn.config,
            secret=secret,
            timeout_seconds=conn.default_timeout_seconds,
        )
        try:
            # HEAD against the base URL; some servers don't support HEAD on "/",
            # so accept any HTTP response (including 4xx) as proof the host
            # answered. We only fail on transport errors.
            await client.request("HEAD", "")
        finally:
            await client.aclose()

    async def _test_ai(self, conn: Connection, secret: str | None) -> None:
        """Unlike _test_rest, actually validate the API key: hit the provider's
        list-models endpoint and require a 2xx. A 401 that _test_rest would
        report as 'Connection successful' is exactly the failure an admin
        setting up an AI provider needs to see. An EMPTY model list is still a
        pass — auth worked (e.g. a fresh Azure resource with no deployments)."""
        if not secret and (conn.config or {}).get("auth_type", "none") != "none":
            raise ValueError(
                "No API key found — check that the credential secret exists in "
                "Admin → Secrets and its name matches this connection's credential reference"
            )
        await self._fetch_ai_models(
            conn.config or {}, secret, conn.default_timeout_seconds, require_models=False)

    async def fetch_provider_models(
        self, db: AsyncSession, *, config: dict, credential_secret_ref: str | None,
        timeout_seconds: int = 30,
    ) -> list[str]:
        """Live model list from a provider, using form-state config (the row
        need not exist yet). Raises ValueError with an admin-readable message."""
        secret = await self.resolve_credential(db, credential_secret_ref)
        if credential_secret_ref and secret is None:
            raise ValueError(
                f"Secret '{credential_secret_ref}' was not found in Admin → Secrets "
                "(or has no value)"
            )
        return await self._fetch_ai_models(config or {}, secret, timeout_seconds)

    async def _fetch_ai_models(
        self, config: dict, secret: str | None, timeout_seconds: int,
        require_models: bool = True,
    ) -> list[str]:
        import httpx

        client = rest_driver.build_client(config, secret=secret, timeout_seconds=timeout_seconds)
        try:
            # Per-preset pagination params (Anthropic defaults to 20/page).
            resp = await client.get(
                providers.ai_models_path(config),
                params=providers.ai_models_query(config) or None,
            )
        except httpx.HTTPError as e:
            raise ValueError(f"Could not reach the provider: {type(e).__name__}: {e}")
        finally:
            await client.aclose()
        if resp.status_code in (401, 403):
            raise ValueError(
                f"The provider rejected the API key (HTTP {resp.status_code}) — "
                "check the credential secret's value"
            )
        if resp.status_code >= 400:
            raise ValueError(
                f"The provider's models endpoint answered HTTP {resp.status_code} — "
                "check the base URL (and models path) for this provider"
            )
        try:
            body = resp.json()
        except Exception:
            raise ValueError("The provider's models endpoint did not return JSON")
        models = providers.parse_models_response(body)
        if not models and require_models:
            raise ValueError(
                "The provider answered but no model ids were found in the response"
            )
        return models

    def _to_response(self, conn: Connection) -> ConnectionResponse:
        return ConnectionResponse(
            id=conn.id,
            name=conn.name,
            description=conn.description or "",
            kind=conn.kind,
            config=_scrub_config_for_response(conn.config or {}),
            credential_secret_ref=conn.credential_secret_ref,
            default_row_limit=conn.default_row_limit,
            default_timeout_seconds=conn.default_timeout_seconds,
            read_only=conn.read_only,
            app_callable=conn.app_callable,
            created_by=conn.created_by,
            created_at=conn.created_at.isoformat(),
            updated_at=conn.updated_at.isoformat(),
        )


# Keys we redact from `config` before returning it on the wire. Admins can
# legitimately put auth-helper params like Trusted_Connection in extra_params,
# but if they paste a literal password / token there we MUST NOT round-trip it.
# Comparison is case-insensitive.
_REDACTED_KEYS = frozenset({
    "pwd", "password", "passwd", "pass", "secret",
    "api_key", "apikey", "access_key", "auth_token", "token",
})

_REDACTED_PLACEHOLDER = "***REDACTED***"


def _scrub_config_for_response(config: dict) -> dict:
    """Return a copy of `config` with password-like keys replaced by a
    placeholder. Recurses into `extra_params` (where pyodbc-style PWD usually
    sneaks in). Case-insensitive match against _REDACTED_KEYS."""
    out: dict = {}
    for k, v in (config or {}).items():
        if isinstance(k, str) and k.lower() in _REDACTED_KEYS and v not in (None, ""):
            out[k] = _REDACTED_PLACEHOLDER
        elif isinstance(v, dict):
            out[k] = _scrub_config_for_response(v)
        else:
            out[k] = v
    return out


connections_service = ConnectionsService()
