---
name: aihub-app-debugging
description: >
  Diagnose and fix AI app-generation failures in AIHub (C:\src\aihub-apps): builds that
  error out, the self-heal loop churning ("Fixing X/8"), Recharts/TypeScript build errors,
  "runtime probe crashed", or "no feedback while generating". Use this BEFORE guessing — it
  gives the exact data-driven diagnostic workflow (generation debug log + traces) and the
  known root causes found the hard way, so you fix the real cause instead of iterating blind.
---

# AIHub app-generation debugging

When an AI-generated app fails to build/verify, or the self-heal loop churns, **do not guess.**
Read the actual data first, then fix the root cause.

## 1. Read the actual data (never guess)

Two sources capture exactly what happened on each build turn:

- **Debug log (richest)** — `<data>/logs/generation_debug.jsonl` (written when `settings.debug`
  is on). One JSON line per event:
  - `turn_start` — the user's prompt + the EXACT system prompts sent
  - `generated` — the raw model output + the generated file contents
  - `verify` — the FULL tsc / build / runtime errors (untruncated)
  - `fix` — the errors fed back + the raw fix response + the new code
  Tail/grep it: `tail -n 80 data/logs/generation_debug.jsonl`. To inspect a turn, read the
  file and `json.loads` each line; filter by `app_id`.
- **Generation traces (DB + UI)** — `GET /api/apps/{id}/traces[/{tid}]`, or the builder's
  History → **Traces** button. Structured timeline of context → generate → verify → fix →
  done/error. Dogfood this to see WHERE a build went wrong.

Latest trace from the DB (run from repo root with `.venv/Scripts/python.exe`):
```python
import asyncio, json
from sqlalchemy import select, desc
from backend.src.database import async_session
from backend.src.generation_trace.models import GenerationTrace
async def main():
    async with async_session() as db:
        t = (await db.execute(select(GenerationTrace).order_by(desc(GenerationTrace.created_at)).limit(1))).scalar_one_or_none()
        print(t.status, t.user_message[:120]); print(json.loads(t.steps_json))
asyncio.run(main())
```

## 2. Known generation root causes (fix directly)

| Symptom | Root cause | Fix |
|---|---|---|
| `Type '(v: number) => ...' is not assignable to type 'Formatter<ValueType, NameType>'` | Recharts v3 types a Tooltip/axis formatter value as `ValueType` (string\|number\|array); `(v: number)` is too narrow → not assignable | `formatter={(value) => ...}` or `(value: any)`; coerce with `Number(value)`. Same for `labelFormatter` / `tickFormatter`. Guidance lives in `ai/prompts.py` SYSTEM_PROMPT — keep it. |
| Build churns "Fixing 1/8 → Runtime issue → Fixing 2/8…" then errors | App calls a dataset that isn't registered → runtime fails; the loop can't fix a *config* gap with *code* | `NO_DATASETS_NOTICE` makes the model use labeled sample data; self-heal early-stops on no-progress / config errors. Tell the user to register a Dataset (Admin → Datasets). |
| `runtime probe crashed:` / `runtime check skipped — probe couldn't run (NotImplementedError)` | uvicorn's Windows **SelectorEventLoop** can't spawn the Chromium subprocess → bare `NotImplementedError` (`str()==""`). **In-process is unfixable**: Playwright's sync API builds its own loop from the global *policy* (still Selector), so "sync API in a thread" AND forcing a Proactor loop in the thread BOTH fail — verified empirically, don't retry them. | **Runs OUT OF PROCESS now.** `verifier.run_runtime_probe` spawns `ai/runtime_probe_child.py` via `subprocess.Popen` (a fresh interpreter gets a Proactor loop where async Playwright launches Chromium); Popen — not an asyncio subprocess — so the parent's Selector loop is irrelevant, and by FILE PATH so it resolves under both `backend.src.ai` and `src.ai`. Shared, import-light bits in `ai/probe_shared.py`. Gated by the **`runtime_probe_enabled`** admin setting (**DEFAULT OFF**; when off, verify stops at boot). A child infra-crash (e.g. Chromium not installed) is still **non-fatal** — passes on tsc/build/boot, surfaced as a "skipped" summary. |
| "No feedback while the AI works" | smart-streaming suppresses the code block, so the transcript looks frozen | `ChatPanel.tsx` shows a persistent "AI is working…" indicator whenever `isStreaming`. |
| RUNTIME: `no such table: X` for SOME app-DB tables while others work; audit log shows `app_db.migrate applied=[] refused=0` on every mount | SDK's `useAppSchema` sent EVERY declaration as `version: 1, name: 'app_schema'`, and `apply_migrations` gated on `version > current` — so only the FIRST declaration ever applied; every other component's schema was silently skipped. Diagnose from `audit_logs` (`resource_id=<app_id>`): the first-ever migrate says `applied=[1]`, all later ones `applied=[]`. | Fixed v0.12.1: migration identity is **(version, name)** tracked per-migration in `_aihub_meta` (`applied_migration.*` keys), and the SDK names each declaration by a content hash (`app_schema_<fnv1a>`), so N declarations at version 1 each apply once. SDK also THROWS on `refused`/`error` in the 200 response (was silent). Existing broken apps self-heal on next preview (re-vendored SDK sends new names → unapplied → idempotent CREATE runs). |

## 3. Operational gotchas (this repo)

- **Intermittent login/API 500 (not 401)** = MULTIPLE `uvicorn ...backend.src.main:app` supervisors
  bound to :8800. Collapse to one: kill procs whose cmdline matches `backend\.src\.main:app` plus
  their spawn-children, then start ONE. Restart from the **repo root** so root `.env`
  (MASTER_ENCRYPTION_KEY) loads → health `encryption_key_source=custom`.
- New routes 404 but green-gate green → the dev backend is STALE; restart it.
- Claude `temperature` is deprecated on newer models → all LLM calls go through
  `backend/src/llm_compat.py` (`acompletion`).
- `.venv/Scripts/python.exe` is a shim that re-execs into `miniconda3\envs\apps` (one interpreter,
  two paths — not two environments).

## 4. Testing gotchas (keep the green-gate green)

- FK enforcement is **ON**. Seed the admin user in a test DIRECTLY via ORM
  (`User(username="admin", display_name="Admin", role="admin")` — Mock-AD users have no password).
  NEVER start the app lifespan via `TestClient` just to seed — that start/stop cycle **deadlocks
  under full-suite state** (hangs ~87%, the 480s gate times out).
- The Playwright runtime probe runs OUT OF PROCESS (subprocess child), which sidesteps both the
  SelectorEventLoop limitation AND the old worker-thread-ProactorEventLoop deadlock (real-Chromium
  teardown in a shared-process loop hung the full suite). Suite tests monkeypatch `run_runtime_probe`
  / `verify_app` and never spawn real Chromium; the gate unit tests stub the subprocess layer.
- Cross-test pollution: `settings.jwt_secret_key` / `database_url` / `app_data_dir` bind at the
  FIRST config import (= some other test module's value in the full run). Resolve paths at call
  time; decode app-issued JWTs with `settings.jwt_secret_key`, not `os.environ`.
- Always finish with the green-gate: `.venv/Scripts/python.exe .claude/hooks/green-gate.py`.

## When you fix a NEW root cause
Add a row to the tables above so the next session doesn't rediscover it.
