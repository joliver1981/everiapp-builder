from pydantic import BaseModel


class UserListResponse(BaseModel):
    id: str
    username: str
    display_name: str
    email: str
    role: str
    is_active: bool
    created_at: str

    class Config:
        from_attributes = True


class RoleUpdateRequest(BaseModel):
    role: str  # admin, developer, user


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "developer"  # admin, developer, user
    display_name: str | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str
