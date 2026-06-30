"""Prompt-library CRUD + first-run seeding of built-in templates."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PromptTemplate
from .schemas import TemplateCreate, TemplateUpdate

# Built-in starter prompts. Seeded once, on first run (when the table is empty).
# Each `body` is a concrete, opinionated prompt that produces a useful first app.
BUILTINS: list[dict] = [
    {
        "id": "builtin-crud-table",
        "title": "CRUD Data Table",
        "category": "Internal tools",
        "description": "An admin table to list, add, edit and delete records in the app's own store.",
        "body": (
            "Build an internal admin tool with a data table. Use the app's own "
            "database (useAppQuery / useAppMutation / useAppSchema). Create a "
            "table with sensible columns, then provide: a searchable/sortable list, "
            "an 'Add' dialog, inline edit, and delete with a confirm step. Show "
            "empty and loading states. Keep the UI clean and keyboard-friendly."
        ),
    },
    {
        "id": "builtin-dashboard",
        "title": "Analytics Dashboard",
        "category": "Analytics",
        "description": "KPI cards plus charts driven by a connected dataset.",
        "body": (
            "Build an analytics dashboard. Use a connected dataset (useDataset) for "
            "the data. Show 3–4 KPI cards at the top (with trend vs. previous "
            "period), then two charts (a time series and a breakdown bar chart), "
            "and a filter bar (date range + one category filter). Make it "
            "responsive and easy to scan."
        ),
    },
    {
        "id": "builtin-kanban",
        "title": "Kanban Board",
        "category": "Internal tools",
        "description": "Drag-and-drop board backed by the app's own store.",
        "body": (
            "Build a Kanban board backed by the app's own database. Columns: To Do, "
            "In Progress, Done. Cards have a title, description and assignee. Support "
            "adding cards, moving them between columns (drag and drop), and editing. "
            "Persist every change with useAppMutation."
        ),
    },
    {
        "id": "builtin-form",
        "title": "Form with Validation",
        "category": "Data entry",
        "description": "A multi-field form with client-side validation and a summary view.",
        "body": (
            "Build a data-entry form with proper validation (required fields, email "
            "and number formats, inline error messages). On submit, save the record "
            "to the app's own store and append it to a list below the form. Include "
            "a reset button and a success toast."
        ),
    },
    {
        "id": "builtin-data-browser",
        "title": "Connected Data Browser",
        "category": "Analytics",
        "description": "Browse and filter rows from a central database dataset (read-only).",
        "body": (
            "Build a read-only data browser over a connected dataset (useDataset). "
            "Provide a paginated table, a text search box, and column-based "
            "filtering. Show row counts and a 'no results' state. Do not attempt to "
            "write — this reads from a central database."
        ),
    },
]


async def seed_builtins(db: AsyncSession) -> int:
    """Insert built-in templates if the table is empty. Returns count inserted.

    Only seeds on a truly empty table, so admin edits/deletions are never undone
    on the next restart.
    """
    total = (await db.execute(select(func.count(PromptTemplate.id)))).scalar_one()
    if total:
        return 0
    for b in BUILTINS:
        db.add(PromptTemplate(
            id=b["id"], title=b["title"], description=b["description"],
            category=b["category"], body=b["body"], is_builtin=True, created_by=None,
        ))
    await db.commit()
    return len(BUILTINS)


async def list_templates(db: AsyncSession) -> list[PromptTemplate]:
    return list((await db.execute(
        select(PromptTemplate).order_by(
            PromptTemplate.is_builtin.desc(),
            PromptTemplate.category.asc(),
            PromptTemplate.title.asc(),
        )
    )).scalars().all())


async def get(db: AsyncSession, tid: str) -> PromptTemplate | None:
    return (await db.execute(
        select(PromptTemplate).where(PromptTemplate.id == tid)
    )).scalar_one_or_none()


async def create(db: AsyncSession, data: TemplateCreate, user_id: str) -> PromptTemplate:
    t = PromptTemplate(
        title=data.title, description=data.description, category=data.category or "general",
        body=data.body, is_builtin=False, created_by=user_id,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def update(db: AsyncSession, tid: str, data: TemplateUpdate) -> PromptTemplate | None:
    t = await get(db, tid)
    if not t:
        return None
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(t, field, value)
    await db.commit()
    await db.refresh(t)
    return t


async def delete(db: AsyncSession, tid: str) -> bool:
    t = await get(db, tid)
    if not t:
        return False
    await db.delete(t)
    await db.commit()
    return True
