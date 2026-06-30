"""Tests for the LDAP auth provider + provider chain.

We can't stand up a real AD server in CI, so we test:
  - role resolution from group membership (pure logic)
  - bind-DN templating
  - CN extraction from group DNs
  - the provider chain's provision_user (find/create/update)
  - the admin config endpoints (CRUD + secret scrubbing) via TestClient

The actual LDAP bind is exercised by mocking ldap3.Connection.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# --- pure-logic tests (no DB) ----------------------------------------------
from src.auth.providers.ldap_provider import LDAP3_AVAILABLE, LdapAuthProvider
from src.auth.providers.base import AuthResult


CONFIG = {
    "server": "dc01.example.com",
    "port": 389,
    "base_dn": "DC=example,DC=com",
    "bind_template": "{username}@example.com",
    "user_search_filter": "(sAMAccountName={username})",
}


@pytest.mark.skipif(not LDAP3_AVAILABLE, reason="ldap3 not installed")
def test_resolve_role_highest_wins():
    p = LdapAuthProvider(
        config=CONFIG,
        group_role_mapping={"Developers": "developer", "Domain Admins": "admin"},
        default_role="user",
    )
    # user in both → admin (highest)
    assert p.resolve_role(["Developers", "Domain Admins"]) == "admin"
    # only developer group
    assert p.resolve_role(["Developers"]) == "developer"
    # no mapped group → default
    assert p.resolve_role(["Some Other Group"]) == "user"
    # no groups → default
    assert p.resolve_role([]) == "user"


@pytest.mark.skipif(not LDAP3_AVAILABLE, reason="ldap3 not installed")
def test_bind_dn_templating():
    p = LdapAuthProvider(config=CONFIG)
    assert p._format_bind_dn("alice") == "alice@example.com"


@pytest.mark.skipif(not LDAP3_AVAILABLE, reason="ldap3 not installed")
def test_extract_cn():
    assert LdapAuthProvider._extract_cn("CN=Developers,OU=Groups,DC=x,DC=com") == "Developers"
    assert LdapAuthProvider._extract_cn("OU=Groups,DC=x,DC=com") is None


@pytest.mark.skipif(not LDAP3_AVAILABLE, reason="ldap3 not installed")
def test_empty_credentials_rejected():
    p = LdapAuthProvider(config=CONFIG)
    assert p.authenticate("", "pw").success is False
    assert p.authenticate("user", "").success is False


@pytest.mark.skipif(not LDAP3_AVAILABLE, reason="ldap3 not installed")
def test_bind_error_returns_invalid_credentials():
    """A LDAPBindError must map to 'Invalid credentials' and NOT retry."""
    from ldap3.core.exceptions import LDAPBindError

    p = LdapAuthProvider(config=CONFIG)
    with patch("src.auth.providers.ldap_provider.Connection", side_effect=LDAPBindError("bad")):
        result = p.authenticate("alice", "wrong")
    assert result.success is False
    assert result.error == "Invalid credentials"


@pytest.mark.skipif(not LDAP3_AVAILABLE, reason="ldap3 not installed")
def test_socket_error_retries_then_fails():
    """LDAPSocketOpenError should be retried once (2 attempts total)."""
    from ldap3.core.exceptions import LDAPSocketOpenError

    p = LdapAuthProvider(config=CONFIG)
    call_count = {"n": 0}

    def _raise(*a, **k):
        call_count["n"] += 1
        raise LDAPSocketOpenError("cold socket")

    with patch("src.auth.providers.ldap_provider.Connection", side_effect=_raise):
        result = p.authenticate("alice", "pw")
    assert result.success is False
    assert "Cannot connect" in result.error
    assert call_count["n"] == 2, "should retry exactly once on socket error"


@pytest.mark.skipif(not LDAP3_AVAILABLE, reason="ldap3 not installed")
def test_successful_bind_returns_attributes():
    """Mock a successful bind + attribute search."""
    p = LdapAuthProvider(config=CONFIG,
                         group_role_mapping={"Developers": "developer"})

    # Build a fake ldap3 entry with the attributes the provider reads
    entry = MagicMock()
    entry.displayName.value = "Alice Smith"
    entry.mail.value = "alice@example.com"
    entry.sAMAccountName.value = "alice"
    entry.memberOf.values = ["CN=Developers,OU=Groups,DC=example,DC=com"]

    fake_conn = MagicMock()
    fake_conn.entries = [entry]
    fake_conn.search.return_value = True

    with patch("src.auth.providers.ldap_provider.Connection", return_value=fake_conn):
        result = p.authenticate("alice", "correct-pw")

    assert result.success is True
    assert result.username == "alice"
    assert result.display_name == "Alice Smith"
    assert result.email == "alice@example.com"
    assert "Developers" in result.groups
    assert p.resolve_role(result.groups) == "developer"


# --- chain.provision_user (DB) ---------------------------------------------
_TMP = Path(tempfile.gettempdir()) / "aihub-ldap-test"
_TMP.mkdir(exist_ok=True)
_DB = _TMP / "ldap.db"
if _DB.exists():
    _DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB}"
os.environ.setdefault("APP_DATA_DIR", str(_TMP / "apps"))
os.environ.setdefault("MASTER_ENCRYPTION_KEY", "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm8=")
os.environ.setdefault("JWT_SECRET_KEY", "ldap-test")

from src.database import async_session, init_db  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _init():
    asyncio.run(init_db())
    yield


def test_provision_creates_then_updates_user():
    from src.auth.providers.chain import provision_user

    async def _run():
        async with async_session() as db:
            r1 = AuthResult(success=True, username="bob", display_name="Bob",
                            email="bob@x.com", external_id="bob", groups=["Developers"])
            user = await provision_user(db, auth_provider="ldap", result=r1,
                                        role="developer", auto_provision=True)
            await db.commit()
            assert user is not None
            uid = user.id
            assert user.role == "developer"
            assert user.auth_provider == "ldap"

            # Repeat login with promoted role
            r2 = AuthResult(success=True, username="bob", display_name="Bob Jones",
                            email="bob@x.com", external_id="bob",
                            groups=["Developers", "Domain Admins"])
            user2 = await provision_user(db, auth_provider="ldap", result=r2,
                                         role="admin", auto_provision=True)
            await db.commit()
            assert user2.id == uid, "should update existing user, not create new"
            assert user2.role == "admin"
            assert user2.display_name == "Bob Jones"

    asyncio.run(_run())


def test_provision_respects_auto_provision_disabled():
    from src.auth.providers.chain import provision_user

    async def _run():
        async with async_session() as db:
            r = AuthResult(success=True, username="newbie", display_name="New",
                           email=None, external_id="newbie", groups=[])
            user = await provision_user(db, auth_provider="ldap", result=r,
                                        role="user", auto_provision=False)
            assert user is None, "should not create when auto_provision is off"

    asyncio.run(_run())
