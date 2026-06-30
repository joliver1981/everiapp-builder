"""Build + persist + read generation traces.

`TraceBuilder` accumulates a run's steps in memory while the generator streams,
then persists ONE row at the end (success, failure, or error) — so a churning or
broken build is fully inspectable afterwards.
"""
from __future__ import annotations

import json
import time

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import GenerationTrace


class TraceBuilder:
    def __init__(self, *, app_id: str, user_id: str | None, user_message: str,
                 system_prompts: list[str], model: str | None, provider: str | None,
                 conversation_id: str | None = None):
        self.app_id = app_id
        self.user_id = user_id or ""
        self.user_message = user_message or ""
        self.system_prompts = list(system_prompts or [])
        self.model = model or ""
        self.provider = provider or ""
        self.conversation_id = conversation_id
        self.steps: list[dict] = []
        self.files_changed: list[dict] = []
        self.status = "running"
        self.summary = ""
        self.verify: dict | None = None
        self.iterations = 0
        self._t0 = time.monotonic()

    def step(self, **kw) -> None:
        self.steps.append(kw)

    def set_files(self, files: list[dict]) -> None:
        self.files_changed = files or []

    def finalize(self, status: str, summary: str = "", verify: dict | None = None) -> None:
        self.status = status
        if summary:
            self.summary = summary
        if verify is not None:
            self.verify = verify

    async def save(self, db: AsyncSession) -> str:
        row = GenerationTrace(
            app_id=self.app_id, conversation_id=self.conversation_id, user_id=self.user_id,
            user_message=self.user_message[:8000], model=self.model, provider=self.provider,
            status=self.status, summary=(self.summary or "")[:2000],
            system_prompts_json=json.dumps(self.system_prompts),
            steps_json=json.dumps(self.steps),
            files_changed_json=json.dumps(self.files_changed),
            verify_json=json.dumps(self.verify),
            duration_seconds=round(time.monotonic() - self._t0, 2),
            iterations=self.iterations,
        )
        db.add(row)
        await db.flush()      # materialize the id before commit (CLAUDE.md pattern)
        trace_id = row.id
        await db.commit()
        return trace_id


def _summary(r: GenerationTrace) -> dict:
    return {
        "id": r.id, "app_id": r.app_id, "user_message": (r.user_message or "")[:200],
        "model": r.model, "provider": r.provider, "status": r.status,
        "summary": r.summary, "iterations": r.iterations,
        "duration_seconds": r.duration_seconds,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "files_changed_count": len(json.loads(r.files_changed_json or "[]")),
    }


def _detail(r: GenerationTrace) -> dict:
    d = _summary(r)
    d.update({
        "conversation_id": r.conversation_id,
        "user_id": r.user_id,
        "system_prompts": json.loads(r.system_prompts_json or "[]"),
        "steps": json.loads(r.steps_json or "[]"),
        "files_changed": json.loads(r.files_changed_json or "[]"),
        "verify": json.loads(r.verify_json or "null"),
    })
    return d


async def list_traces(db: AsyncSession, app_id: str, limit: int = 50) -> list[dict]:
    rows = (await db.execute(
        select(GenerationTrace)
        .where(GenerationTrace.app_id == app_id)
        .order_by(desc(GenerationTrace.created_at))
        .limit(limit)
    )).scalars().all()
    return [_summary(r) for r in rows]


async def get_trace(db: AsyncSession, trace_id: str) -> dict | None:
    r = (await db.execute(
        select(GenerationTrace).where(GenerationTrace.id == trace_id)
    )).scalar_one_or_none()
    return _detail(r) if r else None
