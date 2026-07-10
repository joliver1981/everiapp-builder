import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings
from .database_tuning import apply_index_migrations, apply_sqlite_tuning

logger = logging.getLogger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=settings.db_echo,  # NOT settings.debug — SQL echo floods logs + slows boot
    connect_args={"check_same_thread": False},  # SQLite-specific
)

# Apply WAL + cache_size + busy_timeout + etc. on every new SQLite connection.
# No-op for non-sqlite engines, so this stays safe when we add Postgres later.
apply_sqlite_tuning(engine)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


# Idempotent column additions. SQLite has no real migration system here,
# so we ALTER TABLE ADD COLUMN at startup for any new columns added to
# existing tables. If the column already exists, the error is swallowed.
#
# Format: (table, column, sqlite_type_with_default)
# Always add NEW columns to the END of this list.
_MISSING_COLUMN_MIGRATIONS = [
    ("apps", "bug_widget_enabled",            "BOOLEAN DEFAULT 0"),
    ("apps", "bug_fix_auto_approve_max_risk", "VARCHAR(20) DEFAULT 'none'"),
    ("apps", "ai_verify_level",               "VARCHAR(20) DEFAULT 'tsc_build_boot'"),
    ("apps", "ai_verify_max_iterations",      "INTEGER DEFAULT 8"),
    # Per-column PII tags map (JSON-encoded text), used by audit-log redaction
    ("datasets", "pii_tags",                  "TEXT DEFAULT '{}'"),
    # IdP tracking on users (LDAP/SSO provisioning)
    ("users", "auth_provider",                "VARCHAR(20) DEFAULT 'mock'"),
    ("users", "external_id",                  "VARCHAR(200) DEFAULT ''"),
    # Dataset query-result cache TTL (seconds; 0 = off)
    ("datasets", "cache_ttl_seconds",         "INTEGER DEFAULT 0"),
    # Iframe embedding controls on apps
    ("apps", "embed_enabled",                 "BOOLEAN DEFAULT 0"),
    ("apps", "embed_allowed_origins",         "TEXT DEFAULT ''"),
    # Auto-rollback: consecutive health-probe failures per deployment
    ("deployments", "consecutive_health_failures", "INTEGER DEFAULT 0"),
    # External-marketplace listing identity (set after first publish so
    # re-publishing updates the same listing)
    ("apps", "marketplace_slug",              "VARCHAR(100) DEFAULT ''"),
    # Local-account password hash (username+password auth; empty for SSO/AD)
    ("users", "password_hash",                "VARCHAR(255) DEFAULT ''"),
    # Publisher-authored setup instructions (markdown) for marketplace listings
    ("apps", "setup_instructions",            "TEXT DEFAULT ''"),
    # Last semver published to the external marketplace (bump-button seed)
    ("apps", "last_published_version",        "VARCHAR(20) DEFAULT ''"),
    # Last-published marketplace listing metadata (JSON: short_desc/category/tags/license)
    ("apps", "marketplace_listing",           "TEXT"),
    # Trace spine: join llm_usage rows to ai_spans / a request's trace
    ("llm_usage", "trace_id",                 "VARCHAR(64)"),
    ("llm_usage", "span_id",                  "VARCHAR(36)"),
    # Per-decision LLM timeout before the fallback engages
    ("app_decisions", "timeout_seconds",      "INTEGER DEFAULT 30"),
    # Personal developer "skills" injected into that user's generation turns
    ("users", "dev_standards",                "TEXT DEFAULT ''"),
    # Per-decision LLM output ceiling. NULL = inherit the platform default
    # (`decision_max_output_tokens` platform setting); a value overrides it.
    # No column default: existing rows stay NULL so they inherit.
    ("app_decisions", "max_output_tokens",    "INTEGER"),
    # REST connections an admin has opened up for free-form app calls
    # (callConnection); off by default.
    ("connections", "app_callable",           "BOOLEAN DEFAULT 0"),
]


async def _apply_column_migrations() -> None:
    """Add missing columns to existing tables.

    Idempotent — we PRAGMA table_info first and only ALTER for columns that don't
    yet exist. Avoids the "database is locked" thrash that happens under concurrent
    test runs when ALTER TABLE is fired-and-caught for every column on every init.
    """
    async with engine.begin() as conn:
        # Cache the existing-columns set per table so we don't PRAGMA repeatedly.
        existing: dict[str, set[str]] = {}
        for table, _col, _def in _MISSING_COLUMN_MIGRATIONS:
            if table in existing:
                continue
            try:
                rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
                existing[table] = {r[1] for r in rows}  # row[1] is the column name
            except Exception:
                existing[table] = set()  # table doesn't exist yet; create_all will handle it

        for table, col, definition in _MISSING_COLUMN_MIGRATIONS:
            if col in existing.get(table, set()):
                continue  # already present
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {definition}"))
                logger.info("Added column %s.%s", table, col)
            except Exception as e:
                logger.warning("Column migration %s.%s failed: %s", table, col, e)


async def init_db():
    # Import all models so they register with Base.metadata
    from .auth.models import User, RefreshToken, IdentityProviderConfig  # noqa: F401
    from .apps.models import App, AppVersion, AppPermission, AppSetting, Conversation, Message  # noqa: F401
    from .secrets.models import Secret, AuditLog  # noqa: F401
    from .marketplace.models import MarketplaceListing  # noqa: F401
    from .deployments.models import Deployment, DeploymentTarget  # noqa: F401
    from .bug_reports.models import BugAnalysis, BugReport, FixAttempt  # noqa: F401
    from .connections.models import Connection, AppConnectionBinding  # noqa: F401
    from .datasets.models import Dataset, AppDatasetBinding  # noqa: F401
    from .llm_usage.models import LLMUsage  # noqa: F401
    from .platform_settings.models import PlatformSetting  # noqa: F401
    from .publishing.models import PublishApproval  # noqa: F401
    from .prompt_templates.models import PromptTemplate  # noqa: F401
    from .analytics.models import AppEvent  # noqa: F401
    from .teams.models import Team, TeamMembership  # noqa: F401
    from .generation_trace.models import GenerationTrace  # noqa: F401
    from .tracing.models import AISpan  # noqa: F401
    from .decisions.models import AppDecision, DecisionCache  # noqa: F401
    from .python_packages.models import PythonPackage  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Catch up any existing DB that's missing columns from later code revisions.
    await _apply_column_migrations()

    # Create composite indexes for the hot query paths (audit_logs lookups,
    # message threading, dataset bindings). Idempotent.
    await apply_index_migrations(engine)
