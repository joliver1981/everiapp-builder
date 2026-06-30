import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from .models import App, AppSetting, AppPermission
from ..auth.models import User
from ..config import settings
from ..secrets.encryption import encryption_service
from ..secrets.models import AuditLog
from .schemas import AppCreate, AppUpdate, AppSettingCreate, AppSettingUpdate, AppPermissionCreate

# Setup-wizard field types. `connection` stores a platform Connection id;
# `global_secret` stores a pointer to a global Secret (global_secret_ref).
WIZARD_FIELD_TYPES = {
    "string", "secret", "number", "boolean", "select", "url",
    "connection", "global_secret",
}
_WIZARD_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,99}$")


def validate_wizard(wizard: dict) -> list[str]:
    """Validate a setup-wizard schema. Returns human-readable errors (empty = valid).

    The wizard was previously stored unvalidated (steps: list[dict]), so a typo'd
    type or duplicate key silently broke installs later.
    """
    errors: list[str] = []
    steps = wizard.get("steps")
    if steps is None or not isinstance(steps, list):
        return ["'steps' must be a list"]
    seen_keys: set[str] = set()
    for si, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            errors.append(f"step {si}: must be an object")
            continue
        fields = step.get("fields", [])
        if not isinstance(fields, list):
            errors.append(f"step {si}: 'fields' must be a list")
            continue
        for fi, field in enumerate(fields, 1):
            where = f"step {si}, field {fi}"
            if not isinstance(field, dict):
                errors.append(f"{where}: must be an object")
                continue
            key = field.get("key")
            if not key or not isinstance(key, str):
                errors.append(f"{where}: 'key' is required")
            elif not _WIZARD_KEY_RE.match(key):
                errors.append(f"{where}: key '{key}' must be a valid identifier (letters, digits, _)")
            elif key in seen_keys:
                errors.append(f"{where}: duplicate key '{key}'")
            else:
                seen_keys.add(key)
            ftype = field.get("type", "string")
            if ftype not in WIZARD_FIELD_TYPES:
                errors.append(
                    f"{where}: unknown type '{ftype}' (allowed: {', '.join(sorted(WIZARD_FIELD_TYPES))})"
                )
            if ftype == "select":
                options = field.get("options")
                if not isinstance(options, list) or not options or \
                        not all(isinstance(o, (str, int, float)) for o in options):
                    errors.append(f"{where}: 'select' needs a non-empty 'options' list of values")
    return errors


class AppsService:
    async def list_apps(self, db: AsyncSession, user: User) -> list[App]:
        if user.role == "admin":
            result = await db.execute(select(App).options(selectinload(App.creator)).order_by(App.updated_at.desc()))
        elif user.role == "developer":
            result = await db.execute(
                select(App).options(selectinload(App.creator)).where(App.created_by == user.id).order_by(App.updated_at.desc())
            )
        else:
            # End users see published apps they're allowed to access. An app with
            # NO permission records is open to everyone (backward compatible);
            # otherwise the user must match by id or effective group (AD groups +
            # team membership).
            result = await db.execute(
                select(App).options(selectinload(App.creator)).where(App.status == "published").order_by(App.updated_at.desc())
            )
            published = list(result.scalars().all())
            from ..teams.service import filter_accessible_apps
            return await filter_accessible_apps(db, user, published)
        return list(result.scalars().all())

    async def get_app(self, db: AsyncSession, app_id: str) -> App | None:
        result = await db.execute(select(App).options(selectinload(App.creator)).where(App.id == app_id))
        return result.scalar_one_or_none()

    async def create_app(self, db: AsyncSession, data: AppCreate, user_id: str) -> App:
        app = App(
            name=data.name,
            description=data.description,
            icon=data.icon,
            created_by=user_id,
        )
        db.add(app)
        await db.flush()

        # Scaffold the app from template
        self._scaffold_app(app.id)

        await db.commit()
        await db.refresh(app)
        return app

    async def update_app(
        self,
        db: AsyncSession,
        app_id: str,
        data: AppUpdate,
        *,
        user_id: str | None = None,
    ) -> App | None:
        app = await self.get_app(db, app_id)
        if not app:
            return None

        # Track security-relevant changes so we can audit-log them after the commit.
        # We log bug-widget + auto-approve-risk because they change attack surface
        # (open public intake endpoint) and trust boundaries (autonomous code
        # changes + redeploys without human approval).
        audit_entries: list[tuple[str, str]] = []  # (field, "old -> new")

        if data.name is not None:
            app.name = data.name
        if data.description is not None:
            app.description = data.description
        if data.icon is not None:
            app.icon = data.icon
        if data.ai_toggle_enabled is not None:
            if app.ai_toggle_enabled != data.ai_toggle_enabled:
                audit_entries.append(("ai_toggle_enabled", f"{app.ai_toggle_enabled} -> {data.ai_toggle_enabled}"))
            app.ai_toggle_enabled = data.ai_toggle_enabled
        if data.bug_widget_enabled is not None:
            if app.bug_widget_enabled != data.bug_widget_enabled:
                audit_entries.append(("bug_widget_enabled", f"{app.bug_widget_enabled} -> {data.bug_widget_enabled}"))
            app.bug_widget_enabled = data.bug_widget_enabled
        if data.bug_fix_auto_approve_max_risk is not None:
            if data.bug_fix_auto_approve_max_risk not in ("none", "low", "medium"):
                raise ValueError("bug_fix_auto_approve_max_risk must be one of: none, low, medium")
            if app.bug_fix_auto_approve_max_risk != data.bug_fix_auto_approve_max_risk:
                audit_entries.append((
                    "bug_fix_auto_approve_max_risk",
                    f"{app.bug_fix_auto_approve_max_risk} -> {data.bug_fix_auto_approve_max_risk}",
                ))
            app.bug_fix_auto_approve_max_risk = data.bug_fix_auto_approve_max_risk
        if data.ai_verify_level is not None:
            valid_levels = ("off", "tsc", "tsc_build", "tsc_build_boot",
                            "tsc_build_boot_runtime", "tsc_build_boot_runtime_a11y")
            if data.ai_verify_level not in valid_levels:
                raise ValueError(f"ai_verify_level must be one of: {', '.join(valid_levels)}")
            app.ai_verify_level = data.ai_verify_level
        if data.ai_verify_max_iterations is not None:
            if data.ai_verify_max_iterations < 1 or data.ai_verify_max_iterations > 12:
                raise ValueError("ai_verify_max_iterations must be between 1 and 12")
            app.ai_verify_max_iterations = data.ai_verify_max_iterations
        app.updated_at = datetime.now(timezone.utc)

        for field, change in audit_entries:
            db.add(AuditLog(
                user_id=user_id or "system",
                action=f"app.{field}.change",
                resource_type="app",
                resource_id=app.id,
                details=f"{field}: {change}",
            ))

        await db.commit()
        await db.refresh(app)
        return app

    async def delete_app(self, db: AsyncSession, app_id: str) -> bool:
        app = await self.get_app(db, app_id)
        if not app:
            return False

        # Remove files if they exist
        try:
            app_dir = Path(settings.app_data_dir) / app_id
            if app_dir.exists():
                shutil.rmtree(app_dir)
        except Exception:
            # Continue with deletion even if files can't be removed
            pass

        await db.delete(app)
        await db.commit()
        return True

    def get_file_tree(self, app_id: str) -> list[dict]:
        app_dir = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
        if not app_dir.exists():
            return []
        return self._build_tree(app_dir, app_dir)

    def read_file(self, app_id: str, file_path: str) -> str | None:
        app_dir = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
        full_path = app_dir / file_path

        # Security: ensure the path doesn't escape the app directory
        try:
            full_path.resolve().relative_to(app_dir.resolve())
        except ValueError:
            return None

        if not full_path.exists() or not full_path.is_file():
            return None

        return full_path.read_text(encoding='utf-8')

    def write_file(self, app_id: str, file_path: str, content: str) -> bool:
        app_dir = Path(settings.app_data_dir) / app_id / "draft" / "frontend"
        full_path = app_dir / file_path

        try:
            full_path.resolve().relative_to(app_dir.resolve())
        except ValueError:
            return False

        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding='utf-8')
        return True

    def _scaffold_app(self, app_id: str) -> None:
        """Copy the app template to the app's draft directory."""
        template_dir = Path(__file__).parent.parent.parent.parent / "app-template"
        app_dir = Path(settings.app_data_dir) / app_id / "draft" / "frontend"

        if template_dir.exists():
            shutil.copytree(template_dir, app_dir, dirs_exist_ok=True)
        else:
            # Create minimal structure if template doesn't exist yet
            app_dir.mkdir(parents=True, exist_ok=True)
            src_dir = app_dir / "src"
            src_dir.mkdir(exist_ok=True)

            (src_dir / "App.tsx").write_text(
                'export default function App() {\n'
                '  return (\n'
                '    <div className="min-h-screen bg-zinc-950 p-8 text-zinc-100">\n'
                '      <h1 className="text-2xl font-semibold">New App</h1>\n'
                '      <p className="mt-2 text-zinc-400">Start building by chatting with AI</p>\n'
                '    </div>\n'
                '  )\n'
                '}\n',
                encoding='utf-8',
            )

    def _build_tree(self, root: Path, base: Path) -> list[dict]:
        entries = []
        try:
            for item in sorted(root.iterdir()):
                if item.name in ('node_modules', '.git', 'dist', '__pycache__'):
                    continue
                rel_path = str(item.relative_to(base)).replace('\\', '/')
                if item.is_dir():
                    children = self._build_tree(item, base)
                    entries.append({
                        "name": item.name,
                        "path": rel_path,
                        "type": "directory",
                        "children": children,
                    })
                else:
                    entries.append({
                        "name": item.name,
                        "path": rel_path,
                        "type": "file",
                        "children": [],
                    })
        except PermissionError:
            pass
        return entries

    # ---- App Settings ----

    async def list_settings(self, db: AsyncSession, app_id: str) -> list[AppSetting]:
        result = await db.execute(
            select(AppSetting).where(AppSetting.app_id == app_id).order_by(AppSetting.key)
        )
        return list(result.scalars().all())

    async def _validate_secret_ref(self, db: AsyncSession, secret_ref: str) -> None:
        """A global_secret_ref may only point at an APP-BINDABLE secret.

        Resolved settings return the DECRYPTED value to app viewers, so allowing
        arbitrary refs would let a developer bind a platform credential
        (ai_provider key, agent token, DB password) into their own app and read
        it back in cleartext — privilege escalation past the admin-only boundary.
        """
        from ..secrets.models import Secret, APP_BINDABLE_SECRET_CATEGORIES
        result = await db.execute(select(Secret).where(Secret.id == secret_ref))
        secret = result.scalar_one_or_none()
        if not secret:
            raise ValueError("Referenced global secret not found")
        if secret.category not in APP_BINDABLE_SECRET_CATEGORIES:
            raise ValueError(
                f"Secrets in category '{secret.category}' cannot be bound to apps "
                f"(allowed: {', '.join(sorted(APP_BINDABLE_SECRET_CATEGORIES))})"
            )

    async def create_setting(
        self, db: AsyncSession, app_id: str, data: AppSettingCreate
    ) -> AppSetting:
        # Check for duplicate key
        result = await db.execute(
            select(AppSetting).where(
                and_(AppSetting.app_id == app_id, AppSetting.key == data.key)
            )
        )
        if result.scalar_one_or_none():
            raise ValueError(f"Setting with key '{data.key}' already exists for this app")

        if data.global_secret_ref:
            await self._validate_secret_ref(db, data.global_secret_ref)

        # Encrypt the value if type is secret
        value = data.value
        if value and data.type == "secret":
            value = encryption_service.encrypt(value)

        setting = AppSetting(
            app_id=app_id,
            key=data.key,
            label=data.label,
            type=data.type,
            description=data.description,
            required=data.required,
            default_value=data.default_value,
            value=value,
            global_secret_ref=data.global_secret_ref,
        )
        db.add(setting)
        await db.commit()
        await db.refresh(setting)
        return setting

    async def update_setting(
        self, db: AsyncSession, app_id: str, setting_id: str, data: AppSettingUpdate
    ) -> AppSetting | None:
        result = await db.execute(
            select(AppSetting).where(
                and_(AppSetting.id == setting_id, AppSetting.app_id == app_id)
            )
        )
        setting = result.scalar_one_or_none()
        if not setting:
            return None

        if data.label is not None:
            setting.label = data.label
        if data.description is not None:
            setting.description = data.description
        if data.required is not None:
            setting.required = data.required
        if data.default_value is not None:
            setting.default_value = data.default_value
        if data.value is not None:
            if setting.type == "secret":
                setting.value = encryption_service.encrypt(data.value)
            else:
                setting.value = data.value
        if data.global_secret_ref is not None:
            if data.global_secret_ref:
                await self._validate_secret_ref(db, data.global_secret_ref)
            setting.global_secret_ref = data.global_secret_ref if data.global_secret_ref else None

        await db.commit()
        await db.refresh(setting)
        return setting

    async def delete_setting(self, db: AsyncSession, app_id: str, setting_id: str) -> bool:
        result = await db.execute(
            select(AppSetting).where(
                and_(AppSetting.id == setting_id, AppSetting.app_id == app_id)
            )
        )
        setting = result.scalar_one_or_none()
        if not setting:
            return False
        await db.delete(setting)
        await db.commit()
        return True

    # ---- Setup wizard ----

    async def apply_wizard_values(self, db: AsyncSession, app: App, values: dict) -> int:
        """Upsert AppSettings from setup-wizard answers. Returns fields applied.

        Secret answers are Fernet-encrypted; `global_secret` answers set
        global_secret_ref (a pointer) instead of a value. Does NOT commit —
        callers own the transaction (installs batch this with other writes).
        """
        wizard = app.setup_wizard or {}
        if not values or not wizard.get("steps"):
            return 0
        existing = {s.key: s for s in await self.list_settings(db, app.id)}
        applied = 0
        for step in wizard.get("steps", []):
            for field in step.get("fields", []):
                key = field.get("key")
                if not key or key not in values:
                    continue
                field_type = field.get("type", "string")
                raw = str(values[key])
                value: str | None = None
                secret_ref: str | None = None
                if field_type == "global_secret":
                    secret_ref = raw or None
                    if secret_ref:
                        await self._validate_secret_ref(db, secret_ref)
                elif field_type == "secret" and raw:
                    value = encryption_service.encrypt(raw)
                else:
                    value = raw
                row = existing.get(key)
                if row:
                    row.label = field.get("label", key)
                    row.type = field_type
                    row.required = field.get("required", False)
                    row.value = value
                    row.global_secret_ref = secret_ref
                else:
                    db.add(AppSetting(
                        app_id=app.id,
                        key=key,
                        label=field.get("label", key),
                        type=field_type,
                        description=field.get("description", ""),
                        required=field.get("required", False),
                        value=value,
                        global_secret_ref=secret_ref,
                    ))
                applied += 1
        return applied

    async def get_setup_status(self, db: AsyncSession, app: App) -> dict:
        """Which required setup-wizard fields still lack a value.

        A field is satisfied by a setting row with a value, a global-secret
        pointer, or a default. Drives the post-install "Complete setup" flow.
        """
        wizard = app.setup_wizard or {}
        steps = wizard.get("steps") or []
        if not steps:
            return {"has_wizard": False, "complete": True, "missing": [], "required_total": 0}

        rows = {s.key: s for s in await self.list_settings(db, app.id)}
        missing: list[dict] = []
        required_total = 0
        for step in steps:
            for field in step.get("fields", []):
                if not field.get("required"):
                    continue
                key = field.get("key")
                if not key:
                    continue
                required_total += 1
                row = rows.get(key)
                satisfied = bool(row and (row.value or row.global_secret_ref or row.default_value))
                if not satisfied:
                    missing.append({
                        "key": key,
                        "label": field.get("label", key),
                        "step_title": step.get("title", ""),
                    })
        return {
            "has_wizard": True,
            "complete": not missing,
            "missing": missing,
            "required_total": required_total,
        }

    async def get_resolved_settings(self, db: AsyncSession, app_id: str) -> dict[str, str]:
        """Resolve all settings for runtime injection into the app.
        Decrypts secrets and resolves global_secret_ref pointers."""
        from ..secrets.models import Secret, APP_BINDABLE_SECRET_CATEGORIES
        from ..secrets.encryption import encryption_service as enc

        app_settings = await self.list_settings(db, app_id)
        resolved = {}

        for s in app_settings:
            if s.global_secret_ref:
                # Resolve pointer to global secret. Defense in depth: even a
                # smuggled/legacy ref to a platform credential must never
                # decrypt here — only app-bindable categories resolve.
                ref_result = await db.execute(
                    select(Secret).where(Secret.id == s.global_secret_ref)
                )
                global_secret = ref_result.scalar_one_or_none()
                if (
                    global_secret
                    and global_secret.encrypted_value
                    and global_secret.category in APP_BINDABLE_SECRET_CATEGORIES
                ):
                    resolved[s.key] = enc.decrypt(global_secret.encrypted_value)
                else:
                    resolved[s.key] = s.default_value or ""
            elif s.value:
                if s.type == "secret":
                    resolved[s.key] = enc.decrypt(s.value)
                else:
                    resolved[s.key] = s.value
            elif s.default_value:
                resolved[s.key] = s.default_value
            else:
                resolved[s.key] = ""

        return resolved

    # ---- App Permissions ----

    async def list_permissions(self, db: AsyncSession, app_id: str) -> list[AppPermission]:
        result = await db.execute(
            select(AppPermission).where(AppPermission.app_id == app_id).order_by(AppPermission.created_at)
        )
        return list(result.scalars().all())

    async def add_permission(
        self, db: AsyncSession, app_id: str, data: AppPermissionCreate
    ) -> AppPermission:
        if not data.user_id and not data.group_name:
            raise ValueError("Either user_id or group_name is required")

        # Check for duplicate
        query = select(AppPermission).where(AppPermission.app_id == app_id)
        if data.user_id:
            query = query.where(AppPermission.user_id == data.user_id)
        if data.group_name:
            query = query.where(AppPermission.group_name == data.group_name)
        result = await db.execute(query)
        if result.scalar_one_or_none():
            raise ValueError("Permission already exists for this user/group")

        perm = AppPermission(
            app_id=app_id,
            user_id=data.user_id,
            group_name=data.group_name,
            permission=data.permission,
        )
        db.add(perm)
        await db.commit()
        await db.refresh(perm)
        return perm

    async def remove_permission(self, db: AsyncSession, app_id: str, perm_id: str) -> bool:
        result = await db.execute(
            select(AppPermission).where(
                and_(AppPermission.id == perm_id, AppPermission.app_id == app_id)
            )
        )
        perm = result.scalar_one_or_none()
        if not perm:
            return False
        await db.delete(perm)
        await db.commit()
        return True


apps_service = AppsService()
