from __future__ import annotations

from pydantic import BaseModel

from .models import PromptTemplate


class TemplateCreate(BaseModel):
    title: str
    description: str = ""
    category: str = "general"
    body: str


class TemplateUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    body: str | None = None


class TemplateResponse(BaseModel):
    id: str
    title: str
    description: str
    category: str
    body: str
    is_builtin: bool
    created_by: str | None
    created_at: str
    updated_at: str

    @classmethod
    def of(cls, t: PromptTemplate) -> "TemplateResponse":
        return cls(
            id=t.id, title=t.title, description=t.description, category=t.category,
            body=t.body, is_builtin=t.is_builtin, created_by=t.created_by,
            created_at=t.created_at.isoformat(), updated_at=t.updated_at.isoformat(),
        )
