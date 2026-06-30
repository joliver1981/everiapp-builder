from pydantic import BaseModel


class AppCreate(BaseModel):
    name: str
    description: str = ""
    icon: str = "app-window"


class AppUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    ai_toggle_enabled: bool | None = None
    bug_widget_enabled: bool | None = None
    bug_fix_auto_approve_max_risk: str | None = None  # none | low | medium
    ai_verify_level: str | None = None  # off | tsc | tsc_build | tsc_build_boot
    ai_verify_max_iterations: int | None = None


class AppResponse(BaseModel):
    id: str
    name: str
    description: str
    icon: str
    status: str
    current_version: int
    ai_toggle_enabled: bool
    bug_widget_enabled: bool = False
    bug_fix_auto_approve_max_risk: str = "none"
    ai_verify_level: str = "tsc_build_boot_runtime"
    ai_verify_max_iterations: int = 8
    setup_wizard: dict | None = None
    setup_instructions: str = ""
    last_published_version: str = ""
    installed_from: str | None = None
    created_by: str
    creator_name: str
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class AppFileResponse(BaseModel):
    path: str
    content: str
    language: str


class AppFileTree(BaseModel):
    name: str
    path: str
    type: str  # "file" or "directory"
    children: list["AppFileTree"] = []


# App Settings schemas
class AppSettingCreate(BaseModel):
    key: str
    label: str
    type: str = "string"  # string, secret, number, boolean, select, url
    description: str = ""
    required: bool = False
    default_value: str | None = None
    value: str | None = None
    global_secret_ref: str | None = None


class AppSettingUpdate(BaseModel):
    label: str | None = None
    description: str | None = None
    required: bool | None = None
    default_value: str | None = None
    value: str | None = None
    global_secret_ref: str | None = None


class AppSettingResponse(BaseModel):
    id: str
    app_id: str
    key: str
    label: str
    type: str
    description: str
    required: bool
    default_value: str | None
    value: str | None  # Masked for secret types
    is_set: bool  # Whether a value has been stored
    global_secret_ref: str | None
    created_at: str

    class Config:
        from_attributes = True


class FileWriteRequest(BaseModel):
    content: str


class WizardUpdateRequest(BaseModel):
    steps: list[dict] = []
    title: str | None = None
    description: str | None = None


class SetupApplyRequest(BaseModel):
    """Answers for the app's setup wizard (key -> value)."""
    values: dict = {}


# App Permissions schemas
class AppPermissionCreate(BaseModel):
    user_id: str | None = None
    group_name: str | None = None
    permission: str = "access"  # access, edit


class AppPermissionResponse(BaseModel):
    id: str
    app_id: str
    user_id: str | None
    group_name: str | None
    permission: str
    user_display_name: str | None = None
    created_at: str

    class Config:
        from_attributes = True
