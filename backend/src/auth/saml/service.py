"""SAML flow orchestration. The python3-saml import is lazy so the platform
runs fine without the [saml] extra; the routes surface a clean 501 instead."""
from __future__ import annotations

from fastapi import Request


class SamlNotInstalled(RuntimeError):
    """Raised when a SAML route is hit but python3-saml isn't installed."""


def ensure_saml_deps():
    """Import the SAML toolkit or raise SamlNotInstalled with an install hint."""
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth  # noqa: F401
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise SamlNotInstalled(
            "SAML support is not installed. Run: pip install -e .[saml] "
            "(pulls python3-saml + lxml + xmlsec)."
        ) from e


async def prepare_request_data(request: Request) -> dict:
    """Shape a FastAPI request the way python3-saml's request dict expects."""
    form = {}
    if request.method == "POST":
        try:
            raw = await request.form()
            form = {k: v for k, v in raw.items()}
        except Exception:
            form = {}
    url = request.url
    return {
        "https": "on" if url.scheme == "https" else "off",
        "http_host": url.hostname or "",
        "server_port": str(url.port) if url.port else ("443" if url.scheme == "https" else "80"),
        "script_name": url.path,
        "get_data": dict(request.query_params),
        "post_data": form,
    }


def _auth(req_data: dict, saml_settings: dict):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth
    return OneLogin_Saml2_Auth(req_data, old_settings=saml_settings)


def build_login_redirect(req_data: dict, saml_settings: dict, return_to: str | None) -> str:
    """SP-initiated login: return the IdP redirect URL (with the AuthnRequest)."""
    ensure_saml_deps()
    auth = _auth(req_data, saml_settings)
    return auth.login(return_to=return_to)


def process_acs(req_data: dict, saml_settings: dict) -> tuple[list[str], dict, str | None]:
    """Validate the SAMLResponse. Returns (errors, attributes, name_id)."""
    ensure_saml_deps()
    auth = _auth(req_data, saml_settings)
    auth.process_response()
    errors = auth.get_errors()
    if errors:
        # get_last_error_reason gives a human string for the first error.
        reason = auth.get_last_error_reason() or ""
        return ([f"{e}: {reason}" for e in errors], {}, None)
    if not auth.is_authenticated():
        return (["not_authenticated"], {}, None)
    return ([], auth.get_attributes() or {}, auth.get_nameid())


def sp_metadata(saml_settings: dict) -> tuple[str, list[str]]:
    """Generate SP metadata XML. Returns (xml, validation_errors)."""
    ensure_saml_deps()
    from onelogin.saml2.settings import OneLogin_Saml2_Settings

    settings_obj = OneLogin_Saml2_Settings(saml_settings, sp_validation_only=True)
    metadata = settings_obj.get_sp_metadata()
    errors = settings_obj.validate_metadata(metadata)
    xml = metadata.decode("utf-8") if isinstance(metadata, bytes) else metadata
    return xml, errors
