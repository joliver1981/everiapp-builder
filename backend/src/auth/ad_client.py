"""
Active Directory client with mock mode for development.
In mock mode, provides a set of test users for development.
Swap to real LDAP by changing AD_MODE to "ldap" in settings.
"""

import logging
from dataclasses import dataclass
from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class ADUser:
    username: str
    display_name: str
    email: str
    groups: list[str]


# Mock users for development
MOCK_USERS = {
    "admin": ADUser(
        username="admin",
        display_name="Admin User",
        email="admin@aihub.local",
        groups=["AIHub-Admins", "AIHub-Developers"],
    ),
    "developer": ADUser(
        username="developer",
        display_name="Dev User",
        email="developer@aihub.local",
        groups=["AIHub-Developers"],
    ),
    "user": ADUser(
        username="user",
        display_name="Regular User",
        email="user@aihub.local",
        groups=[],
    ),
}

# All mock users use password "password" in dev mode
MOCK_PASSWORD = "password"


class ADClient:
    def authenticate(self, username: str, password: str) -> ADUser | None:
        if settings.ad_mode == "mock":
            return self._mock_authenticate(username, password)
        return self._ldap_authenticate(username, password)

    def _mock_authenticate(self, username: str, password: str) -> ADUser | None:
        user = MOCK_USERS.get(username)
        if user and password == MOCK_PASSWORD:
            return user
        return None

    def _ldap_authenticate(self, username: str, password: str) -> ADUser | None:
        """Authenticate against Active Directory using ldap3."""
        try:
            from ldap3 import Server, Connection, ALL, SUBTREE
        except ImportError:
            logger.error("ldap3 is not installed. Run: pip install ldap3")
            return None

        if not settings.ad_host or not settings.ad_base_dn:
            logger.error("AD_HOST and AD_BASE_DN must be configured for LDAP mode")
            return None

        try:
            server = Server(
                settings.ad_host,
                get_info=ALL,
                use_ssl=settings.ad_use_ssl,
            )

            # Build user DN
            user_dn = f"{settings.ad_bind_dn_prefix}{username},{settings.ad_base_dn}"

            conn = Connection(server, user=user_dn, password=password, auto_bind=True)

            # Search for user to get attributes
            search_base = settings.ad_user_search_base or settings.ad_base_dn
            conn.search(
                search_base,
                f"(sAMAccountName={username})",
                search_scope=SUBTREE,
                attributes=["displayName", "mail", "memberOf"],
            )

            if not conn.entries:
                conn.unbind()
                return None

            entry = conn.entries[0]
            display_name = str(entry.displayName) if hasattr(entry, 'displayName') else username
            email = str(entry.mail) if hasattr(entry, 'mail') else f"{username}@{settings.ad_host}"

            # Extract group names from memberOf
            groups = []
            if hasattr(entry, 'memberOf'):
                for dn in entry.memberOf:
                    # Extract CN from DN like "CN=GroupName,OU=Groups,DC=example,DC=com"
                    dn_str = str(dn)
                    if dn_str.startswith("CN="):
                        group_name = dn_str.split(",")[0][3:]
                        groups.append(group_name)

            conn.unbind()
            return ADUser(
                username=username,
                display_name=display_name,
                email=email,
                groups=groups,
            )

        except Exception as e:
            logger.exception("LDAP authentication failed for user %s: %s", username, e)
            return None

    def test_connection(self) -> dict:
        """Test the LDAP connection. Returns a dict with success, message, and optional info."""
        if settings.ad_mode == "mock":
            return {"success": True, "message": "Mock mode — no real AD connection needed", "mode": "mock"}

        try:
            from ldap3 import Server, Connection, ALL
        except ImportError:
            return {"success": False, "message": "ldap3 is not installed"}

        if not settings.ad_host:
            return {"success": False, "message": "AD_HOST is not configured"}

        try:
            server = Server(settings.ad_host, get_info=ALL, use_ssl=settings.ad_use_ssl)
            conn = Connection(server, auto_bind=True) if not settings.ad_bind_dn else Connection(
                server,
                user=settings.ad_bind_dn,
                password=settings.ad_bind_password,
                auto_bind=True,
            )
            info = {
                "host": settings.ad_host,
                "ssl": settings.ad_use_ssl,
                "base_dn": settings.ad_base_dn,
                "server_type": str(server.info.vendor_name) if server.info else "unknown",
            }
            conn.unbind()
            return {"success": True, "message": "Connection successful", "info": info}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def search_users(self, query: str, limit: int = 20) -> list[dict]:
        """Search AD for users matching a query. Only works in LDAP mode."""
        if settings.ad_mode == "mock":
            results = []
            for u in MOCK_USERS.values():
                if query.lower() in u.username.lower() or query.lower() in u.display_name.lower():
                    results.append({"username": u.username, "display_name": u.display_name, "email": u.email})
            return results[:limit]

        try:
            from ldap3 import Server, Connection, ALL, SUBTREE
            server = Server(settings.ad_host, get_info=ALL, use_ssl=settings.ad_use_ssl)
            conn = Connection(
                server,
                user=settings.ad_bind_dn,
                password=settings.ad_bind_password,
                auto_bind=True,
            )
            search_base = settings.ad_user_search_base or settings.ad_base_dn
            conn.search(
                search_base,
                f"(&(objectClass=user)(|(sAMAccountName=*{query}*)(displayName=*{query}*)))",
                search_scope=SUBTREE,
                attributes=["sAMAccountName", "displayName", "mail"],
                size_limit=limit,
            )
            results = []
            for entry in conn.entries:
                results.append({
                    "username": str(entry.sAMAccountName),
                    "display_name": str(entry.displayName) if hasattr(entry, 'displayName') else "",
                    "email": str(entry.mail) if hasattr(entry, 'mail') else "",
                })
            conn.unbind()
            return results
        except Exception as e:
            logger.exception("LDAP search failed: %s", e)
            return []

    def get_user_role(self, ad_user: ADUser) -> str:
        if self._group_matches(settings.ad_admin_group, ad_user.groups):
            return "admin"
        if self._group_matches(settings.ad_developer_group, ad_user.groups):
            return "developer"
        return "user"

    def _group_matches(self, config_group: str, user_groups: list[str]) -> bool:
        """Match group name — handles both simple names and full DN format."""
        # Extract CN from DN if present (e.g. "CN=AIHub-Admins,OU=..." → "AIHub-Admins")
        cn = config_group
        if cn.upper().startswith("CN="):
            cn = cn.split(",")[0][3:]
        return config_group in user_groups or cn in user_groups


ad_client = ADClient()
