import logging
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from .config import settings, validate_settings_for_production
from .database import init_db
from .middleware import setup_middleware

# Configure structured logging
_log_format = (
    '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
    if not settings.debug
    else "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
)
logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format=_log_format,
    stream=sys.stdout,
)

# Even in debug mode, silence chatty third-party loggers that emit a line PER DB
# operation / file event. At DEBUG they flood the backend console with tens of
# thousands of lines (it buried real tracebacks and slowed boot) — our own app
# modules stay at the configured level so genuine errors stay visible.
for _noisy in ("aiosqlite", "sqlalchemy.engine", "sqlalchemy.pool", "watchfiles"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

_startup_time: float = 0
from .auth.router import router as auth_router
from .apps.router import router as apps_router
from .ai.router import router as ai_router, rewind_router as ai_rewind_router
from .secrets.router import router as secrets_router
from .ai_providers.router import router as ai_providers_router
from .admin.router import router as admin_router
from .versions.router import router as versions_router
from .runtime.router import api_router as runtime_api_router, proxy_router as runtime_proxy_router
from .ai_toggle.router import router as ai_toggle_router
from .marketplace.router import router as marketplace_router
from .packaging.router import router as packaging_router
from .deployments.router import (
    admin_router as deployments_admin_router,
    deployments_router,
)
from .bug_reports.router import (
    admin_router as bug_reports_admin_router,
    public_router as bug_reports_public_router,
)
from .connections.router import router as connections_router
from .licensing.router import router as license_router
from .app_db.router import router as app_db_router
from .security_scan.router import router as security_scan_router
from .dependency_scan.router import router as dependency_scan_router
from .llm_usage.router import router as llm_usage_router
from .auth.providers.router import router as auth_providers_router
from .platform_settings.router import router as platform_settings_router
from .publishing.router import (
    router as publishing_router,
    admin_router as publishing_admin_router,
)
from .prompt_templates.router import (
    router as prompt_templates_router,
    admin_router as prompt_templates_admin_router,
)
from .analytics.router import (
    router as analytics_router,
    admin_router as analytics_admin_router,
)
from .siem.router import admin_router as siem_admin_router
from .notifications.router import admin_router as notifications_admin_router
from .setup.router import router as setup_router
from .audit_search.router import router as audit_search_router
from .system_status.router import router as system_status_router
from .backups.router import admin_router as backups_admin_router
from .teams.router import admin_router as teams_admin_router
from .ai_prompts.router import admin_router as ai_prompts_admin_router
from .generation_trace.router import router as generation_trace_router
from .tracing.router import router as tracing_router
from .decisions.router import router as decisions_router
from .embedding.router import router as embedding_router
from .auth.saml.router import router as saml_router
from .auth.oidc.router import router as oidc_router
from .datasets.router import (
    router as datasets_router,
    introspection_router as datasets_introspection_router,
    runtime_router as datasets_runtime_router,
    bindings_router as datasets_bindings_router,
    discoverable_router as datasets_discoverable_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_time
    import asyncio
    validate_settings_for_production()

    # Put the vendored Node (packaged installs) on PATH before any app build or
    # preview spawns npm/npx. No-op in dev checkouts (uses system Node).
    from .node_env import ensure_on_path
    ensure_on_path()

    # Apply a staged restore BEFORE the DB engine connects (init_db is the first
    # connection). Safe: never overwrites the live DB while it's open.
    try:
        from .backups.service import apply_pending_restore
        if apply_pending_restore():
            logger.info("Applied a staged backup restore on startup.")
    except Exception:
        logger.exception("startup restore check failed (non-fatal)")

    await init_db()
    _startup_time = time.time()

    # First-run seed of the built-in prompt library (no-op once non-empty).
    from .prompt_templates.service import seed_builtins
    from .database import async_session
    try:
        async with async_session() as _seed_db:
            await seed_builtins(_seed_db)
    except Exception:
        logger.exception("prompt-template seeding failed (non-fatal)")

    from .deployments.service import health_loop as deployments_health_loop
    from .audit_rotation import audit_rotation_loop
    from .siem.forwarder import siem_forwarder_loop
    from .backups.service import backup_loop
    from .tracing.service import retention_loop as trace_retention_loop
    from .tracing.writer import span_writer
    health_task = asyncio.create_task(deployments_health_loop())
    audit_task = asyncio.create_task(audit_rotation_loop())
    siem_task = asyncio.create_task(siem_forwarder_loop())
    backup_task = asyncio.create_task(backup_loop())
    span_writer.start()
    trace_retention_task = asyncio.create_task(trace_retention_loop())

    logger.info("AIHub Platform started (debug=%s)", settings.debug)
    try:
        yield
    finally:
        health_task.cancel()
        audit_task.cancel()
        siem_task.cancel()
        backup_task.cancel()
        trace_retention_task.cancel()
        # Flush queued spans so a clean shutdown doesn't lose them.
        await span_writer.stop()
        # Shutdown: stop all running app processes
        from .runtime.manager import runtime_manager
        from .runtime.proxy import close_client
        await runtime_manager.shutdown_all()
        await close_client()


app = FastAPI(
    title="AIHub Platform",
    description="AI-powered app development and deployment platform",
    version="0.2.0",
    lifespan=lifespan,
)

setup_middleware(app)

# Mount routers
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(saml_router, prefix="/api/auth/saml", tags=["saml"])
app.include_router(oidc_router, prefix="/api/auth/oidc", tags=["oidc"])
app.include_router(apps_router, prefix="/api/apps", tags=["apps"])
app.include_router(ai_router, prefix="/api/ai", tags=["ai"])
app.include_router(ai_rewind_router, prefix="/api/apps", tags=["rewind"])
app.include_router(secrets_router, prefix="/api/secrets", tags=["secrets"])
app.include_router(ai_providers_router, prefix="/api/admin/ai-providers", tags=["ai-providers"])
app.include_router(admin_router, prefix="/api/admin", tags=["admin"])
app.include_router(versions_router, prefix="/api/apps", tags=["versions"])
app.include_router(runtime_api_router, prefix="/api/apps", tags=["runtime"])
app.include_router(runtime_proxy_router, prefix="/apps", tags=["runtime-proxy"])
app.include_router(ai_toggle_router, prefix="/api/ai-toggle", tags=["ai-toggle"])
app.include_router(marketplace_router, prefix="/api/marketplace", tags=["marketplace"])
app.include_router(packaging_router, prefix="/api/apps", tags=["packaging"])
app.include_router(deployments_admin_router, prefix="/api/admin", tags=["deployments-admin"])
app.include_router(deployments_router, prefix="/api", tags=["deployments"])
app.include_router(bug_reports_public_router, prefix="/api/bug-reports", tags=["bug-reports-public"])
app.include_router(bug_reports_admin_router, prefix="/api/bug-reports", tags=["bug-reports-admin"])
app.include_router(connections_router, prefix="/api/admin/connections", tags=["connections"])
app.include_router(license_router, prefix="/api/admin/license", tags=["license"])
app.include_router(ai_prompts_admin_router, prefix="/api/admin/ai", tags=["ai-prompts"])
app.include_router(generation_trace_router, prefix="/api/apps", tags=["generation-trace"])
app.include_router(tracing_router, prefix="/api/apps", tags=["tracing"])
app.include_router(decisions_router, prefix="/api/decisions", tags=["decisions"])
app.include_router(app_db_router, prefix="/api/apps", tags=["app-db"])
app.include_router(security_scan_router, prefix="/api/apps", tags=["security-scan"])
app.include_router(dependency_scan_router, prefix="/api/apps", tags=["dependency-scan"])
app.include_router(llm_usage_router, prefix="/api/admin/llm-usage", tags=["llm-usage"])
app.include_router(auth_providers_router, prefix="/api/admin/auth-providers", tags=["auth-providers"])
app.include_router(platform_settings_router, prefix="/api/admin/settings", tags=["platform-settings"])
app.include_router(publishing_router, prefix="/api/apps", tags=["publishing"])
app.include_router(publishing_admin_router, prefix="/api/admin", tags=["publishing-admin"])
app.include_router(prompt_templates_router, prefix="/api/prompt-templates", tags=["prompt-templates"])
app.include_router(prompt_templates_admin_router, prefix="/api/admin/prompt-templates", tags=["prompt-templates-admin"])
app.include_router(analytics_router, prefix="/api/apps", tags=["analytics"])
app.include_router(analytics_admin_router, prefix="/api/admin", tags=["analytics-admin"])
app.include_router(siem_admin_router, prefix="/api/admin/siem", tags=["siem"])
app.include_router(notifications_admin_router, prefix="/api/admin/notifications", tags=["notifications"])
app.include_router(setup_router, prefix="/api/setup", tags=["setup"])
app.include_router(audit_search_router, prefix="/api/admin", tags=["audit-search"])
app.include_router(system_status_router, prefix="/api/admin/system", tags=["system-status"])
app.include_router(backups_admin_router, prefix="/api/admin/backups", tags=["backups"])
app.include_router(teams_admin_router, prefix="/api/admin/teams", tags=["teams"])
app.include_router(embedding_router, prefix="/api/apps", tags=["embedding"])
app.include_router(datasets_introspection_router, prefix="/api/admin/connections", tags=["connections-introspect"])
app.include_router(datasets_router, prefix="/api/admin/datasets", tags=["datasets"])
app.include_router(datasets_runtime_router, prefix="/api/apps", tags=["datasets-runtime"])
app.include_router(datasets_bindings_router, prefix="/api/apps", tags=["datasets-bindings"])
app.include_router(datasets_discoverable_router, prefix="/api/datasets", tags=["datasets-discoverable"])


@app.get("/api/health")
async def health_check():
    from sqlalchemy import text
    from .database import async_session
    from .secrets.encryption import encryption_service
    from .runtime.manager import runtime_manager

    # Check database connectivity
    db_ok = True
    try:
        async with async_session() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    uptime = round(time.time() - _startup_time) if _startup_time else 0
    running_apps = sum(
        1 for p in runtime_manager._processes.values() if p.status == "running"
    )

    # Return 503 (not 200) when the DB is unreachable so load balancers, the Docker
    # healthcheck, and NSSM-style monitors that test the status code actually see the
    # instance as unhealthy instead of trusting a body field they don't parse.
    return JSONResponse(
        status_code=200 if db_ok else 503,
        content={
            "status": "healthy" if db_ok else "degraded",
            "version": "0.2.0",
            "debug": settings.debug,
            "uptime_seconds": uptime,
            "database": "ok" if db_ok else "error",
            "encryption_key_source": encryption_service.key_source,
            "running_apps": running_apps,
        },
    )


# Sanitize unhandled exceptions in production (no stack traces leaked)
if not settings.debug:
    @app.exception_handler(Exception)
    async def _sanitized_error_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal error occurred."},
        )


# --- Static SPA serving (single-container / installer deployments) ----------
# In dev, Vite serves the frontend on :5173 and proxies /api here, so the dist
# folder doesn't exist and this block is skipped. In the Docker image (and the
# eventual installer) the built SPA is copied to frontend/dist and the backend
# serves it from the same origin — no separate web server needed. A catch-all
# returns index.html for client-side routes so deep links / refresh work.
def _mount_spa_if_present() -> None:
    from pathlib import Path
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    # Packaged installs (PyInstaller) set AIHUB_SPA_DIR to the bundled dist,
    # since __file__-relative resolution doesn't hold once frozen. Otherwise
    # fall back to the repo layout: backend/src/main.py → parents[2]/frontend/dist.
    import os
    env_dir = os.environ.get("AIHUB_SPA_DIR")
    dist = Path(env_dir) if env_dir else Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not dist.is_dir() or not (dist / "index.html").is_file():
        logger.info("No frontend/dist found; SPA static serving disabled (dev mode)")
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    index_file = str(dist / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_catch_all(full_path: str):
        # Never shadow API or runtime-proxy routes.
        if full_path.startswith(("api/", "apps/")):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(index_file)

    logger.info("Serving built SPA from %s", dist)


_mount_spa_if_present()
