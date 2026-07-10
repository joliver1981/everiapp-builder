import logging
import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _load_yaml_config() -> dict:
    """Load operator config from a YAML file if present.

    Resolution order for the path:
      1. AIHUB_CONFIG_FILE env var
      2. %ProgramData%\\AIHub\\config.yaml  (Windows)
      3. /etc/aihub/aihub.yaml              (Linux)

    Returns a flat dict of lowercase keys → values that Settings reads as
    defaults (env vars still win — see Settings below). Missing file or missing
    PyYAML → empty dict (env/.env still work). This gives IT a single file they
    can mass-deploy via SCCM / Ansible / Group Policy.
    """
    candidates = []
    env_path = os.environ.get("AIHUB_CONFIG_FILE")
    if env_path:
        candidates.append(Path(env_path))
    program_data = os.environ.get("ProgramData")
    if program_data:
        candidates.append(Path(program_data) / "AIHub" / "config.yaml")
    candidates.append(Path("/etc/aihub/aihub.yaml"))

    for p in candidates:
        try:
            if p.is_file():
                import yaml  # optional dep
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    logger.info("Loaded operator config from %s", p)
                    return {str(k).lower(): v for k, v in data.items()}
        except ImportError:
            logger.warning("PyYAML not installed; cannot read %s. pip install pyyaml", p)
            return {}
        except Exception as e:
            logger.warning("Failed to read config file %s: %s", p, e)
    return {}


_YAML_CONFIG = _load_yaml_config()

_JWT_DEFAULT_SECRET = "dev-secret-change-in-production"


class Settings(BaseSettings):
    # Database
    database_url: str = "sqlite+aiosqlite:///./data/aihub.db"
    # SQL query echo (VERY verbose). Off by default and deliberately NOT tied to
    # `debug` — `debug` also gates debug_log + health flags, but SQL echo floods
    # startup with tens of thousands of lines and noticeably slows boot (it was a
    # factor in "backend didn't answer in 90s"). Opt in with DB_ECHO=true.
    db_echo: bool = False

    # JWT
    jwt_secret_key: str = _JWT_DEFAULT_SECRET
    jwt_algorithm: str = "HS256"
    # Access-token TTL. 48h (2880 min) for an on-prem internal tool: long enough
    # that an idle session doesn't silently expire mid-use — that expiry was what
    # surfaced as a confusing "Unauthorized" in builder panels when the refresh
    # also failed. Trade-off: access tokens aren't server-revocable like refresh
    # tokens, which is acceptable on-prem. Lower it (e.g. 60) + rely on silent
    # refresh if you need tighter revocation. Override via JWT_ACCESS_TOKEN_EXPIRE_MINUTES.
    jwt_access_token_expire_minutes: int = 2880
    jwt_refresh_token_expire_days: int = 30

    # Encryption
    master_encryption_key: str = ""  # If empty, auto-derived from machine ID

    # Transport security
    # `cookie_secure` controls the Secure flag on the refresh-token cookie:
    #   None  → auto: Secure when the request arrived over HTTPS. This works for
    #           both a plain-HTTP lab and a TLS / reverse-proxy production install
    #           (run uvicorn with --proxy-headers, or terminate TLS at a proxy that
    #           sets X-Forwarded-Proto, so the request scheme is seen as https).
    #   True  → always Secure (the cookie is dropped on any plain-HTTP request).
    #   False → never Secure (only for an HTTP-only lab you accept the risk on).
    cookie_secure: bool | None = None
    # Emit Strict-Transport-Security on HTTPS responses. No effect on plain HTTP,
    # so it is safe to leave enabled; disable only if a parent proxy sets its own.
    hsts_enabled: bool = True

    @field_validator("cookie_secure", mode="before")
    @classmethod
    def _blank_cookie_secure_is_auto(cls, v):
        # An empty env var (COOKIE_SECURE= in .env, or compose's ${COOKIE_SECURE:-})
        # means "unset" → fall back to auto-detect, not a bool-parse error at boot.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    # CORS
    cors_origins: list[str] = ["http://localhost:5173"]

    # App Runtime
    app_data_dir: str = "./data/apps"
    app_base_port: int = 9000
    app_max_instances: int = 50
    node_path: str = ""  # Override auto-detected node path
    runtime_startup_timeout: int = 30

    # Active Directory
    ad_mode: str = "mock"  # "mock" or "ldap"
    ad_host: str = ""
    ad_base_dn: str = ""
    ad_bind_dn: str = ""
    ad_bind_password: str = ""
    ad_user_search_base: str = ""
    ad_admin_group: str = "AIHub-Admins"
    ad_developer_group: str = "AIHub-Developers"
    ad_bind_dn_prefix: str = "CN="  # Prefix for user DN construction
    ad_use_ssl: bool = True

    # Server
    host: str = "0.0.0.0"
    port: int = 8800
    debug: bool = False

    # Deployments
    vite_aihub_base_url: str = ""  # baked into deployed app builds; empty = same-origin
    agent_request_timeout: int = 30
    ssh_connect_timeout: int = 15
    deployer_command_timeout: int = 600  # npm install / build
    pip_command_timeout: int = 600  # admin server-function package installs (pip)
    deployment_cors_allow_pattern: str = ""  # extra regex for CORS (e.g. "https?://192\\.168\\.\\d+\\.\\d+(:\\d+)?")

    # Audit log rotation
    # Rows older than `audit_retention_months` get exported to gzipped JSONL
    # under `audit_archive_dir` (defaults to `<data_dir>/archives/`), then
    # deleted from the live table. Disable rotation by setting enabled=False.
    audit_rotation_enabled: bool = True
    audit_retention_months: int = 24
    audit_archive_dir: str = ""  # empty = derive from database_url's directory
    audit_rotation_interval_hours: int = 24  # how often the background loop checks

    # SIEM forwarding: how often the background loop pushes new audit events to
    # the configured SIEM endpoint. Endpoint/transport/auth live in runtime
    # platform settings (toggleable without a restart).
    siem_flush_interval_seconds: int = 30

    # extra="ignore": the .env is operator-editable and legitimately carries keys
    # that aren't Settings fields (e.g. HOST_PORT/PORT consumed by run_server.py,
    # or future runtime vars). Without this, pydantic-settings parses the whole
    # .env file and raises extra_forbidden on the first unknown key, bricking the
    # service at boot — exactly what HOST_PORT in the installer .env did.
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


# YAML config supplies defaults; real environment variables still override them
# (precedence: env var > .env file > YAML config file > code default). We pass
# YAML values as kwargs but drop any key that's already set in the real
# environment so the env var wins.
_yaml_kwargs = {
    k: v for k, v in _YAML_CONFIG.items()
    if k in Settings.model_fields and k.upper() not in os.environ and k not in os.environ
}
settings = Settings(**_yaml_kwargs)


def validate_settings_for_production() -> None:
    """Validate critical settings. Called at startup."""
    if not settings.debug and settings.jwt_secret_key == _JWT_DEFAULT_SECRET:
        raise SystemExit(
            "\n[FATAL] JWT_SECRET_KEY is still the default value.\n"
            "Set a strong secret in your .env file before running in production:\n"
            '  JWT_SECRET_KEY="your-random-secret-here"\n'
            "Or set DEBUG=true for development mode.\n"
        )
    if settings.debug and settings.jwt_secret_key == _JWT_DEFAULT_SECRET:
        logger.warning(
            "Using default JWT secret — this is fine for development but must be changed for production."
        )
