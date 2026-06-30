from pydantic import BaseModel, field_validator


def _validate_username(v: str) -> str:
    v = v.strip()
    if not (3 <= len(v) <= 100):
        raise ValueError("Username must be 3–100 characters")
    if not all(c.isalnum() or c in "._-" for c in v):
        raise ValueError("Username may contain letters, digits, and . _ - only")
    return v


def _validate_password(v: str) -> str:
    if len(v) < 8:
        raise ValueError("Password must be at least 8 characters")
    return v


class LoginRequest(BaseModel):
    username: str
    password: str


class BootstrapAdminRequest(BaseModel):
    """First-run creation of the initial administrator account."""
    username: str
    password: str

    _u = field_validator("username")(_validate_username)
    _p = field_validator("password")(_validate_password)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

    _p = field_validator("new_password")(_validate_password)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    email: str
    role: str
    groups: list[str]
    created_at: str

    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse
