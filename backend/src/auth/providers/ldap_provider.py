"""LDAP / Active Directory authentication provider.

Ported from a production-tested LDAP implementation and adapted to AIHub's
string-role model. The battle-scar mitigations are preserved verbatim because
they were discovered the hard way in production:

  BUG-AUTH-001 (cold-path timeout): default connect_timeout is 30s, not 10s,
    because the very first LDAP login after process start has to do DNS + TCP +
    TLS handshake + RootDSE fetch all at once.

  BUG-AUTH-001 (cold-path retry): a single retry on LDAPSocketOpenError ONLY
    (never on LDAPBindError) — the first connection after startup occasionally
    fails on the cold socket but succeeds on retry. We never retry bad-password
    binds, to avoid AD account lockout.

  TLS validate=CERT_NONE: enterprise AD commonly uses self-signed / internal-CA
    certs; we don't fail the handshake on them.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

try:
    import ldap3
    from ldap3 import ALL, Connection, Server, SUBTREE, Tls
    from ldap3.core.exceptions import (
        LDAPBindError,
        LDAPException,
        LDAPSocketOpenError,
    )
    LDAP3_AVAILABLE = True
except ImportError:
    LDAP3_AVAILABLE = False

from .base import AuthResult, BaseAuthProvider

logger = logging.getLogger(__name__)

# AIHub role precedence — used to pick the "highest" role when a user is in
# multiple mapped groups.
_ROLE_RANK = {"user": 1, "developer": 2, "admin": 3}


class LdapAuthProvider(BaseAuthProvider):
    """Authenticate users via LDAP bind against AD or any LDAP server.

    Config dict shape (stored as JSON in IdentityProviderConfig.config_json):
        {
          "server": "dc01.company.com",
          "port": 389,
          "use_ssl": false,
          "base_dn": "DC=company,DC=com",
          "bind_template": "{username}@company.com",
          "user_search_filter": "(sAMAccountName={username})",
          "user_search_base": "DC=company,DC=com",
          "attributes": ["displayName", "mail", "memberOf", "sAMAccountName"],
          "connect_timeout": 30,
          "receive_timeout": 10
        }

    group_role_mapping maps AD group CN → AIHub role string, e.g.:
        {"Domain Admins": "admin", "Developers": "developer"}
    """

    def __init__(self, config: dict[str, Any], group_role_mapping: Optional[dict[str, str]] = None,
                 default_role: str = "user"):
        if not LDAP3_AVAILABLE:
            raise ImportError("ldap3 is not installed. Run: pip install ldap3")
        self._config = config
        self._group_role_mapping = group_role_mapping or {}
        self._default_role = default_role

    @property
    def provider_type(self) -> str:
        return "ldap"

    # ----- server / connection construction -------------------------------
    def _get_server(self) -> "ldap3.Server":
        host = self._config["server"]
        use_ssl = self._config.get("use_ssl", False)
        port = self._config.get("port", 636 if use_ssl else 389)
        # 30s default — cold path needs the headroom (BUG-AUTH-001).
        connect_timeout = self._config.get("connect_timeout", 30)

        tls = None
        if use_ssl:
            import ssl
            tls = Tls(validate=ssl.CERT_NONE)  # accept enterprise self-signed certs

        return Server(host, port=port, use_ssl=use_ssl, tls=tls,
                      get_info=ALL, connect_timeout=connect_timeout)

    def _format_bind_dn(self, username: str) -> str:
        template = self._config.get("bind_template", "{username}")
        return template.replace("{username}", username)

    @staticmethod
    def _extract_cn(dn: str) -> Optional[str]:
        for part in dn.split(","):
            part = part.strip()
            if part.upper().startswith("CN="):
                return part[3:]
        return None

    def _search_user_attributes(self, conn: "ldap3.Connection", username: str) -> dict[str, Any]:
        search_base = self._config.get("user_search_base", self._config.get("base_dn", ""))
        search_filter = self._config.get("user_search_filter", "(sAMAccountName={username})")
        search_filter = search_filter.replace("{username}", username)
        attributes = self._config.get(
            "attributes", ["displayName", "mail", "memberOf", "sAMAccountName"]
        )

        fallback = {"display_name": username, "email": None,
                    "sam_account_name": username, "groups": []}
        try:
            conn.search(search_base=search_base, search_filter=search_filter,
                        search_scope=SUBTREE, attributes=attributes)
            if not conn.entries:
                logger.warning("LDAP user search returned no results for: %s", username)
                return fallback

            entry = conn.entries[0]
            attrs: dict[str, Any] = {}
            attrs["display_name"] = (
                str(entry.displayName.value)
                if hasattr(entry, "displayName") and entry.displayName.value
                else username
            )
            attrs["email"] = (
                str(entry.mail.value)
                if hasattr(entry, "mail") and entry.mail.value else None
            )
            attrs["sam_account_name"] = (
                str(entry.sAMAccountName.value)
                if hasattr(entry, "sAMAccountName") and entry.sAMAccountName.value
                else username
            )
            groups = []
            if hasattr(entry, "memberOf") and entry.memberOf.values:
                for group_dn in entry.memberOf.values:
                    cn = self._extract_cn(str(group_dn))
                    if cn:
                        groups.append(cn)
            attrs["groups"] = groups
            return attrs
        except Exception as e:
            logger.warning("LDAP user attribute search failed: %s", e)
            return fallback

    def resolve_role(self, groups: list[str]) -> str:
        """Map AD groups → AIHub role. Highest-ranked match wins."""
        if not self._group_role_mapping or not groups:
            return self._default_role
        best = self._default_role
        for group_name, role in self._group_role_mapping.items():
            if group_name in groups and _ROLE_RANK.get(role, 0) > _ROLE_RANK.get(best, 0):
                best = role
        return best

    # ----- authenticate ----------------------------------------------------
    def authenticate(self, username: str, password: str) -> AuthResult:
        if not username or not password:
            return AuthResult(success=False, error="Username and password are required")

        bind_dn = self._format_bind_dn(username)
        receive_timeout = self._config.get("receive_timeout", 10)

        max_attempts = 2  # retry-once on socket error only (BUG-AUTH-001)
        last_socket_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            server = self._get_server()  # fresh per attempt; no network until Connection()
            try:
                t0 = time.monotonic()
                conn = Connection(server, user=bind_dn, password=password,
                                  auto_bind=True, receive_timeout=receive_timeout,
                                  read_only=True)
                logger.info(
                    "LDAP bind ok for %s (attempt %d/%d, %.2fs)",
                    username, attempt, max_attempts, time.monotonic() - t0,
                )
                attrs = self._search_user_attributes(conn, username)
                conn.unbind()
                return AuthResult(
                    success=True,
                    username=attrs["sam_account_name"],
                    display_name=attrs["display_name"],
                    email=attrs.get("email"),
                    external_id=attrs["sam_account_name"],
                    groups=attrs.get("groups", []),
                )

            except LDAPBindError:
                # Bad credentials — never retry (avoids AD lockout).
                logger.info("LDAP bind failed for %s (attempt %d)", username, attempt)
                return AuthResult(success=False, error="Invalid credentials")

            except LDAPSocketOpenError as e:
                logger.warning(
                    "LDAP socket-open failed (attempt %d/%d) for %s: %s",
                    attempt, max_attempts, username, e,
                )
                last_socket_error = e
                if attempt < max_attempts:
                    continue  # retry with a fresh server + connection
                return AuthResult(success=False,
                                  error=f"Cannot connect to LDAP server: {e}")

            except LDAPException as e:
                logger.error("LDAP error for %s (attempt %d): %s", username, attempt, e)
                return AuthResult(success=False, error=f"LDAP error: {e}")

            except Exception as e:
                logger.error("Unexpected LDAP auth error for %s: %s", username, e)
                return AuthResult(success=False, error=f"Authentication error: {e}")

        return AuthResult(success=False,
                          error=f"LDAP authentication exhausted retries: {last_socket_error}")

    def test_connection(self) -> tuple[bool, str]:
        try:
            server = self._get_server()
            conn = Connection(server, auto_bind=False, receive_timeout=5)
            conn.open()
            ok = conn.bound or bool(server.info)
            conn.unbind()
            where = f"{self._config['server']}:{self._config.get('port', 389)}"
            return (True, f"Connected to LDAP server at {where}") if ok else \
                   (True, "Connection opened successfully")
        except LDAPSocketOpenError as e:
            return (False, f"Cannot connect to LDAP server: {e}")
        except Exception as e:
            return (False, f"Connection test failed: {e}")
