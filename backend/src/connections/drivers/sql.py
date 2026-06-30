"""Async SQLAlchemy URL builder + driver registry for SQL connections.

Each dialect maps to an async driver. SQLite is bundled (aiosqlite is already a
project dep). Other dialects rely on optional drivers that the operator can
install on demand — if the driver is missing, we surface a clean error in the
test-connection response instead of crashing.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus


@dataclass(frozen=True)
class SqlDialect:
    name: str               # user-facing dialect name (postgres, mssql, etc.)
    sqla_scheme: str        # sqlalchemy URL scheme (postgresql+asyncpg, etc.)
    required_module: str    # python module that must import successfully
    default_port: int | None = None


# Order matters only for UI hints; runtime lookup is by `name`.
DIALECTS: dict[str, SqlDialect] = {
    "sqlite":   SqlDialect("sqlite",   "sqlite+aiosqlite",    "aiosqlite", None),
    "postgres": SqlDialect("postgres", "postgresql+asyncpg",  "asyncpg",   5432),
    "mysql":    SqlDialect("mysql",    "mysql+aiomysql",      "aiomysql",  3306),
    "mssql":    SqlDialect("mssql",    "mssql+aioodbc",       "aioodbc",   1433),
    "oracle":   SqlDialect("oracle",   "oracle+oracledb",     "oracledb",  1521),
}


class DriverNotInstalledError(RuntimeError):
    """Raised when the python driver for a dialect is not importable."""


def get_dialect(name: str) -> SqlDialect:
    if name not in DIALECTS:
        raise ValueError(f"Unknown SQL dialect '{name}'. Known: {sorted(DIALECTS)}")
    return DIALECTS[name]


def ensure_driver(dialect: SqlDialect) -> None:
    """Raise DriverNotInstalledError if the dialect's python driver isn't importable."""
    try:
        importlib.import_module(dialect.required_module)
    except ImportError as e:
        raise DriverNotInstalledError(
            f"Driver '{dialect.required_module}' not installed for dialect "
            f"'{dialect.name}'. Install it via the admin UI or `pip install {dialect.required_module}`."
        ) from e


# Preference order when the operator didn't name an ODBC driver. 17 first: it's
# the most compatible (Driver 18 defaults Encrypt=yes, which breaks connections
# to internal SQL Servers with self-signed / no certs unless extra params are set).
_ODBC_DRIVER_PREFERENCE = (
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 18 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)


def default_odbc_driver() -> str:
    """Pick an installed ODBC Driver for SQL Server, else a sensible name.

    Without a DRIVER in the ODBC connect string, pyodbc fails with IM002
    ('Data source name not found and no default driver specified'). MSSQL
    connections created via the UI rarely specify one, so we auto-fill it.
    """
    installed: list[str] = []
    try:
        import pyodbc
        installed = list(pyodbc.drivers())
    except Exception:
        installed = []
    for name in _ODBC_DRIVER_PREFERENCE:
        if name in installed:
            return name
    # Last resort: prefer any installed "for SQL Server" driver, else a default name.
    for name in installed:
        if "sql server" in name.lower():
            return name
    return "ODBC Driver 17 for SQL Server"


def build_url(
    config: dict,
    *,
    password: Optional[str] = None,
) -> str:
    """Build a SQLAlchemy async URL from a Connection.config + decrypted password.

    Expected keys in `config`:
      dialect:        one of DIALECTS
      host:           hostname (ignored for sqlite)
      port:           optional override of dialect default
      database:       db name (or path for sqlite)
      username:       optional db user
      extra_params:   optional dict appended as ?k=v&...
    """
    dialect_name = config.get("dialect")
    if not dialect_name:
        raise ValueError("config.dialect is required")
    d = get_dialect(dialect_name)

    if d.name == "sqlite":
        # sqlite uses a file path (or ":memory:") in the `database` slot.
        path = config.get("database", ":memory:")
        return f"{d.sqla_scheme}:///{path}"

    user = config.get("username", "")
    host = config.get("host", "")
    port = config.get("port") or d.default_port
    db = config.get("database", "")
    auth = ""
    if user:
        pwd_part = f":{quote_plus(password)}" if password else ""
        auth = f"{quote_plus(user)}{pwd_part}@"
    netloc = host
    if port:
        netloc = f"{host}:{port}"
    url = f"{d.sqla_scheme}://{auth}{netloc}/{db}"

    extra = dict(config.get("extra_params") or {})
    # MSSQL via ODBC NEEDS a DRIVER or pyodbc fails with IM002. The connection
    # UI rarely sets one, so default it to an installed ODBC driver.
    if d.name == "mssql" and not any(str(k).lower() == "driver" for k in extra):
        extra["driver"] = default_odbc_driver()
    if extra:
        qs = "&".join(f"{quote_plus(str(k))}={quote_plus(str(v))}" for k, v in extra.items())
        url = f"{url}?{qs}"
    return url
