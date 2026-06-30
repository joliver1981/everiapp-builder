"""Pure SAML helpers — no python3-saml / lxml / xmlsec import here.

Everything in this module is plain-Python and unit-testable without the crypto
stack installed: build the python3-saml settings dict from a stored provider
config, map SAML assertion attributes to an identity, and resolve a role from
group membership (highest-rank-wins, same policy as the LDAP provider).
"""
from __future__ import annotations

from ..providers.base import AuthResult
from ..providers.roles import resolve_role  # noqa: F401  (re-exported for callers/tests)

_POST_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
_REDIRECT_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
_NAMEID_UNSPECIFIED = "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"

# Default attribute names cover both AD FS / Azure claim URIs and short names.
_DEFAULT_ATTR_MAP = {
    "username": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "email": "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "display_name": "http://schemas.xmlsoap.org/identity/claims/displayname",
    "groups": "http://schemas.xmlsoap.org/claims/Group",
}


def default_sp_urls(base_url: str, provider_id: str) -> dict:
    base = base_url.rstrip("/")
    return {
        "entity_id": f"{base}/api/auth/saml/{provider_id}/metadata",
        "acs_url": f"{base}/api/auth/saml/{provider_id}/acs",
    }


def build_saml_settings(config: dict, base_url: str, provider_id: str) -> dict:
    """Construct the python3-saml settings dict from a stored provider config."""
    sp = default_sp_urls(base_url, provider_id)
    sp_entity = config.get("sp_entity_id") or sp["entity_id"]
    sp_acs = config.get("sp_acs_url") or sp["acs_url"]

    return {
        "strict": bool(config.get("strict", True)),
        "debug": False,
        "sp": {
            "entityId": sp_entity,
            "assertionConsumerService": {"url": sp_acs, "binding": _POST_BINDING},
            "NameIDFormat": config.get("name_id_format") or _NAMEID_UNSPECIFIED,
            "x509cert": config.get("sp_x509_cert", "") or "",
            "privateKey": config.get("sp_private_key", "") or "",
        },
        "idp": {
            "entityId": config.get("idp_entity_id", ""),
            "singleSignOnService": {
                "url": config.get("idp_sso_url", ""),
                "binding": _REDIRECT_BINDING,
            },
            "x509cert": config.get("idp_x509_cert", "") or "",
        },
        "security": {
            "wantAssertionsSigned": bool(config.get("want_assertions_signed", True)),
            "wantMessagesSigned": bool(config.get("want_messages_signed", False)),
            "requestedAuthnContext": False,
        },
    }


def validate_saml_config(config: dict) -> list[str]:
    """Return a list of human-readable problems with the IdP config (empty = ok)."""
    problems = []
    for key, label in (("idp_entity_id", "IdP entity ID"),
                       ("idp_sso_url", "IdP SSO URL"),
                       ("idp_x509_cert", "IdP signing certificate")):
        if not (config.get(key) or "").strip():
            problems.append(f"Missing {label} ({key})")
    return problems


def _first(value):
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def extract_identity(attributes: dict, name_id: str | None, mapping: dict | None) -> AuthResult:
    """Map a SAML assertion's attributes + NameID to an AuthResult."""
    m = {**_DEFAULT_ATTR_MAP, **(mapping or {})}
    attrs = attributes or {}

    username = _first(attrs.get(m["username"])) or name_id or ""
    email = _first(attrs.get(m["email"]))
    display = _first(attrs.get(m["display_name"])) or username
    groups = _as_list(attrs.get(m["groups"]))
    external_id = name_id or username

    return AuthResult(
        success=bool(username),
        username=str(username),
        display_name=str(display or username),
        email=str(email) if email else None,
        external_id=str(external_id),
        groups=groups,
    )


# resolve_role is re-exported from ..providers.roles (shared with OIDC).
