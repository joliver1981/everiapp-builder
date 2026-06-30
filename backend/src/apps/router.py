from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from ..database import get_db
from ..auth.dependencies import get_current_user, require_role
from ..auth.models import User
from .schemas import (
    AppCreate, AppUpdate, AppResponse,
    AppSettingCreate, AppSettingUpdate, AppSettingResponse,
    AppPermissionCreate, AppPermissionResponse,
    FileWriteRequest, WizardUpdateRequest, SetupApplyRequest,
)
from .service import apps_service
from ..runtime.manager import runtime_manager

router = APIRouter()


def _app_to_response(app, creator=None) -> AppResponse:
    """Convert an App model to an AppResponse."""
    name = "Unknown"
    if creator:
        name = creator.display_name if hasattr(creator, 'display_name') else str(creator)
    elif app.creator:
        name = app.creator.display_name
    return AppResponse(
        id=app.id,
        name=app.name,
        description=app.description,
        icon=app.icon,
        status=app.status,
        current_version=app.current_version,
        ai_toggle_enabled=app.ai_toggle_enabled,
        # These were previously dropped, so the UI always saw the schema
        # defaults (verify level / bug-widget flags never reflected the saved
        # value). Pass them through so settings round-trip correctly.
        bug_widget_enabled=app.bug_widget_enabled,
        bug_fix_auto_approve_max_risk=app.bug_fix_auto_approve_max_risk,
        ai_verify_level=app.ai_verify_level,
        ai_verify_max_iterations=app.ai_verify_max_iterations,
        setup_wizard=app.setup_wizard,
        setup_instructions=app.setup_instructions or "",
        last_published_version=app.last_published_version or "",
        installed_from=app.installed_from,
        created_by=app.created_by,
        creator_name=name,
        created_at=app.created_at.isoformat(),
        updated_at=app.updated_at.isoformat(),
    )


@router.get("", response_model=list[AppResponse])
async def list_apps(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    apps = await apps_service.list_apps(db, user)
    return [_app_to_response(app) for app in apps]


@router.post("", response_model=AppResponse, status_code=status.HTTP_201_CREATED)
async def create_app(
    body: AppCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    app = await apps_service.create_app(db, body, user.id)
    return _app_to_response(app, user)


@router.get("/{app_id}", response_model=AppResponse)
async def get_app(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    creator = app.creator if app.creator else None
    return _app_to_response(app, creator)


@router.put("/{app_id}", response_model=AppResponse)
async def update_app(
    app_id: str,
    body: AppUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    try:
        app = await apps_service.update_app(db, app_id, body, user_id=user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    creator = app.creator if app.creator else None
    return _app_to_response(app, creator)


@router.delete("/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_app(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    # Stop the runtime if it's running before deleting files
    await runtime_manager.stop_app(app_id)

    deleted = await apps_service.delete_app(db, app_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="App not found")


@router.get("/{app_id}/files")
async def get_file_tree(
    app_id: str,
    user: User = Depends(require_role("admin", "developer")),
):
    return apps_service.get_file_tree(app_id)


# ---- Last-known-good draft snapshot (set automatically before each AI turn) ----


@router.get("/{app_id}/lkg")
async def get_lkg_info(
    app_id: str,
    user: User = Depends(require_role("admin", "developer")),
):
    """Return metadata about the last-known-good snapshot for this app, if any.
    Drives the 'Roll back to last-known-good' button visibility in the UI."""
    from ..ai.snapshots import snapshot_info
    info = snapshot_info(app_id)
    return {"has_snapshot": info is not None, "info": info}


@router.post("/{app_id}/rollback-draft")
async def rollback_draft(
    app_id: str,
    user: User = Depends(require_role("admin", "developer")),
):
    """Restore the draft from the last-known-good snapshot (taken before the most
    recent AI turn). Used when AI-generated changes failed verification and the
    user wants to undo them without re-asking the AI."""
    from ..ai.snapshots import has_snapshot, restore
    if not has_snapshot(app_id):
        raise HTTPException(status_code=404, detail="No last-known-good snapshot for this app")
    ok = restore(app_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Rollback failed")
    return {"restored": True}


@router.get("/{app_id}/files/{file_path:path}")
async def read_file(
    app_id: str,
    file_path: str,
    user: User = Depends(require_role("admin", "developer")),
):
    content = apps_service.read_file(app_id, file_path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")

    # Determine language from extension
    ext = file_path.rsplit('.', 1)[-1] if '.' in file_path else ''
    lang_map = {'tsx': 'typescript', 'ts': 'typescript', 'js': 'javascript', 'css': 'css', 'json': 'json', 'py': 'python', 'md': 'markdown'}

    return {
        "path": file_path,
        "content": content,
        "language": lang_map.get(ext, 'plaintext'),
    }


@router.put("/{app_id}/files/{file_path:path}")
async def write_file(
    app_id: str,
    file_path: str,
    body: FileWriteRequest,
    user: User = Depends(require_role("admin", "developer")),
):
    content = body.content
    success = apps_service.write_file(app_id, file_path, content)
    if not success:
        raise HTTPException(status_code=400, detail="Failed to write file")
    return {"ok": True}


# ---- Setup Wizard Endpoints ----

@router.get("/{app_id}/wizard")
async def get_wizard(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return app.setup_wizard or {}


@router.put("/{app_id}/wizard")
async def update_wizard(
    app_id: str,
    body: WizardUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    wizard = body.model_dump()
    from .service import validate_wizard
    errors = validate_wizard(wizard)
    if errors:
        raise HTTPException(status_code=400, detail="Invalid wizard: " + "; ".join(errors))
    app.setup_wizard = wizard
    await db.commit()
    return app.setup_wizard


@router.get("/{app_id}/setup-status")
async def get_setup_status(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Whether the app's required setup-wizard fields have values — drives the
    post-install "Complete setup" prompt. Same access rule as resolved settings."""
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    if user.role not in ("admin", "developer"):
        from ..teams.service import can_access_app
        if not await can_access_app(db, user, app):
            raise HTTPException(status_code=403, detail="No access to this app")
    return await apps_service.get_setup_status(db, app)


@router.post("/{app_id}/setup")
async def apply_setup(
    app_id: str,
    body: SetupApplyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    """Apply (or re-run) the app's setup wizard: upsert settings from answers.
    Secrets are encrypted; global_secret answers become pointers."""
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    if not (app.setup_wizard or {}).get("steps"):
        raise HTTPException(status_code=400, detail="This app has no setup wizard")
    try:
        applied = await apps_service.apply_wizard_values(db, app, body.values)
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    await db.commit()
    status_after = await apps_service.get_setup_status(db, app)
    return {"applied": applied, **status_after}


# ---- App Settings Endpoints ----

def _setting_to_response(s) -> AppSettingResponse:
    """Convert an AppSetting model to a response, masking secret values."""
    value = s.value
    is_set = value is not None and value != ""
    if s.type == "secret" and value:
        value = "••••••••"
    return AppSettingResponse(
        id=s.id,
        app_id=s.app_id,
        key=s.key,
        label=s.label,
        type=s.type,
        description=s.description,
        required=s.required,
        default_value=s.default_value,
        value=value,
        is_set=is_set,
        global_secret_ref=s.global_secret_ref,
        created_at=s.created_at.isoformat(),
    )


@router.get("/{app_id}/settings", response_model=list[AppSettingResponse])
async def list_settings(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    settings_list = await apps_service.list_settings(db, app_id)
    return [_setting_to_response(s) for s in settings_list]


@router.post("/{app_id}/settings", response_model=AppSettingResponse, status_code=status.HTTP_201_CREATED)
async def create_setting(
    app_id: str,
    body: AppSettingCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    # Verify app exists
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    try:
        setting = await apps_service.create_setting(db, app_id, body)
        return _setting_to_response(setting)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{app_id}/settings/{setting_id}", response_model=AppSettingResponse)
async def update_setting(
    app_id: str,
    setting_id: str,
    body: AppSettingUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    try:
        setting = await apps_service.update_setting(db, app_id, setting_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not setting:
        raise HTTPException(status_code=404, detail="Setting not found")
    return _setting_to_response(setting)


@router.delete("/{app_id}/settings/{setting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_setting(
    app_id: str,
    setting_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    deleted = await apps_service.delete_setting(db, app_id, setting_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Setting not found")


@router.get("/{app_id}/settings/resolved")
async def get_resolved_settings(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get fully resolved settings (decrypted, refs resolved) — used by app runtime.

    Any authenticated user who can ACCESS the app may read its resolved config:
    the running app consumes these values client-side via the SDK, so anyone
    permitted to run the app necessarily receives them. (Previously this was
    admin/developer-only, which 401/403'd the SDK for regular users.)
    """
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    if user.role not in ("admin", "developer"):
        from ..teams.service import can_access_app
        if not await can_access_app(db, user, app):
            raise HTTPException(status_code=403, detail="No access to this app")
    return await apps_service.get_resolved_settings(db, app_id)


@router.get("/{app_id}/widget-config")
async def get_widget_config(
    app_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Public, no-auth runtime metadata the SDK needs to decide which widgets to mount.

    Intentionally returns only safe-to-expose flags (no settings, no permissions data).
    """
    from sqlalchemy import select
    from .models import App
    result = await db.execute(select(App).where(App.id == app_id))
    app = result.scalar_one_or_none()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return {
        "app_id": app.id,
        "ai_toggle_enabled": app.ai_toggle_enabled,
        "bug_widget_enabled": app.bug_widget_enabled,
    }


# ---- App Permissions Endpoints ----

@router.get("/{app_id}/permissions", response_model=list[AppPermissionResponse])
async def list_permissions(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    perms = await apps_service.list_permissions(db, app_id)
    responses = []
    for p in perms:
        # Resolve user display name if user_id is set
        display_name = None
        if p.user_id:
            from ..auth.models import User as UserModel
            from sqlalchemy import select
            u_result = await db.execute(select(UserModel).where(UserModel.id == p.user_id))
            u = u_result.scalar_one_or_none()
            if u:
                display_name = u.display_name
        responses.append(AppPermissionResponse(
            id=p.id,
            app_id=p.app_id,
            user_id=p.user_id,
            group_name=p.group_name,
            permission=p.permission,
            user_display_name=display_name or p.group_name,
            created_at=p.created_at.isoformat(),
        ))
    return responses


@router.post("/{app_id}/permissions", response_model=AppPermissionResponse, status_code=status.HTTP_201_CREATED)
async def add_permission(
    app_id: str,
    body: AppPermissionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    app = await apps_service.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    try:
        perm = await apps_service.add_permission(db, app_id, body)
        return AppPermissionResponse(
            id=perm.id,
            app_id=perm.app_id,
            user_id=perm.user_id,
            group_name=perm.group_name,
            permission=perm.permission,
            user_display_name=perm.group_name,
            created_at=perm.created_at.isoformat(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{app_id}/permissions/{perm_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_permission(
    app_id: str,
    perm_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("admin", "developer")),
):
    deleted = await apps_service.remove_permission(db, app_id, perm_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Permission not found")
