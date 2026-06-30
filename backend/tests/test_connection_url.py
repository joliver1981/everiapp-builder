"""SQL connection URL building — esp. the MSSQL ODBC driver default.

Regression: an MSSQL connection created via the UI (no `driver` in extra_params)
produced a URL with no DRIVER, so pyodbc failed with IM002 ('Data source name
not found and no default driver specified'). build_url must auto-fill a driver.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from src.connections.drivers.sql import build_url, default_odbc_driver


def _qs(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_mssql_defaults_a_driver_when_missing():
    url = build_url({
        "dialect": "mssql", "host": "dbhost", "database": "appdb",
        "username": "dbuser",
    }, password="secret")
    assert url.startswith("mssql+aioodbc://")
    q = _qs(url)
    assert "driver" in q, "MSSQL URL must include a driver (else pyodbc IM002)"
    assert q["driver"] == default_odbc_driver()
    assert "sql server" in q["driver"].lower()


def test_mssql_respects_explicit_driver():
    url = build_url({
        "dialect": "mssql", "host": "h", "database": "db", "username": "u",
        "extra_params": {"driver": "ODBC Driver 18 for SQL Server", "TrustServerCertificate": "yes"},
    }, password="p")
    q = _qs(url)
    assert q["driver"] == "ODBC Driver 18 for SQL Server"
    assert q["TrustServerCertificate"] == "yes"


def test_mssql_explicit_driver_case_insensitive_key():
    # If the operator used 'Driver' (capital D), we must not also inject 'driver'.
    url = build_url({
        "dialect": "mssql", "host": "h", "database": "db",
        "extra_params": {"Driver": "SQL Server"},
    })
    q = _qs(url)
    drivers = [k for k in q if k.lower() == "driver"]
    assert len(drivers) == 1
    assert q.get("Driver") == "SQL Server"


def test_default_odbc_driver_is_installed_or_sane():
    name = default_odbc_driver()
    assert "sql server" in name.lower()


def test_other_dialects_get_no_driver():
    pg = build_url({"dialect": "postgres", "host": "h", "database": "db", "username": "u"}, password="p")
    assert "driver=" not in pg.lower()
    assert pg.startswith("postgresql+asyncpg://")


def test_sqlite_path():
    url = build_url({"dialect": "sqlite", "database": "/tmp/x.db"})
    assert url == "sqlite+aiosqlite:////tmp/x.db"
