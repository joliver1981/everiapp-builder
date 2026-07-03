# AIHub — agent rules

## Project shape

- `backend/` — FastAPI control plane (`backend/src/`), pytest in `backend/tests/`
- `aihub-agent/` — Python deployment daemon installed on target hosts
- `frontend/` — React 19 + Vite + Tailwind v4 + shadcn/ui
- `app-template/` — Scaffold for AI-generated apps
- `app-sdk/` — SDK package the generated apps consume
- `.venv/` — single Python venv shared by backend + aihub-agent
- `start.bat` — launches backend (`:8800`), frontend (`:5173`), agent (`:8765`)

## Green-gate (Stop hook)

`.claude/hooks/green-gate.py` runs on every Stop. It executes, in order:

1. `pytest -q` in `backend/`
2. `pytest -q` in `aihub-agent/`
3. `tsc --noEmit` in `frontend/`

If anything fails, the hook returns exit 2 with the failed command's output and **blocks turn-end**. You must fix the failure and re-verify before declaring done. Run it yourself any time with:

```
.venv/Scripts/python.exe .claude/hooks/green-gate.py
```

The gate exists because we've burned cycles on bugs that pass unit tests but break at the HTTP/Pydantic/dependency layer. See the rule below.

## When you touch an HTTP endpoint

**Write an integration test that hits the real route via FastAPI `TestClient`.** Unit-testing the service method directly is not enough — it skips request validation, dependency injection, response serialization, and SQLAlchemy session lifecycle differences. Bugs we missed by doing service-only tests:

- `audit_logs.resource_id` NOT NULL violation on `POST /api/admin/deployment-targets` — passed every service-level test, blew up on the first real HTTP call

Pattern:

```python
from fastapi.testclient import TestClient
from src.main import app

def test_create_target_via_http():
    with TestClient(app) as client:
        # log in, get a token
        # POST the actual endpoint
        # assert 200 and that the side effects landed
```

If the endpoint requires auth, log in through `/api/auth/login` first (admin/password works in dev) and pass the bearer token. Don't bypass auth — that's part of what you're testing.

## When you add a SQLAlchemy model with `default=lambda: ...`

Those defaults run at **flush time**, not at `__init__`. If you need the auto-generated `.id` before commit (e.g., to write an `AuditLog` referencing the new row), call `await db.flush()` first. The bug listed above was exactly this.

Existing pattern to follow: see `backend/src/ai_providers/service.py:create_provider` — flush between insert and audit-log write.

## Run-the-app workflow

- One-time setup is in `start.bat` (installs venv, agent, frontend node_modules on demand).
- For testing deploys: agent runs on `:8765` with dev token `aihub-dev-token`. Add a Secret with that value (category `agent_token`), then a Deployment Target pointing at `localhost:8765`.
- For testing bug-report flow: enable the bug widget on an app in the builder top bar, deploy it, file a report via the floating red button on the deployed app.

## External database drivers (for platform Connections)

SQLite is bundled. Other dialects are opt-in extras declared in `backend/pyproject.toml`:

| Dialect | Install                              | Notes |
|---------|--------------------------------------|-------|
| postgres | `pip install -e .[postgres]`        | uses `asyncpg` |
| mysql    | `pip install -e .[mysql]`           | uses `aiomysql` |
| mssql    | `pip install -e .[mssql]`           | uses `aioodbc` + `pyodbc`; ALSO requires the OS-level ODBC Driver 17 (or 18) for SQL Server |
| oracle   | `pip install -e .[oracle]`          | uses `oracledb` |
| all      | `pip install -e .[all-dbs]`         | every dialect at once |

If a Connection's dialect doesn't have its Python driver installed, the "Test connection" UI surfaces a clean error pointing at the right install command — connections, datasets, and runtime execute all check via `connections.drivers.sql.ensure_driver()`.

**MSSQL regression tests** (`tests/test_data/test_mssql_http.py`) are skipped automatically when `pyodbc` isn't importable or when no local SQL Server is reachable at `localhost` with Windows auth on the `AIHUB_TEST_MSSQL_DB` database (default `LLMDB`). When you do have the local instance running, they lock in the cross-dialect row-cap mechanism — see [this comment](backend/src/datasets/runtime.py) for why we use `fetchmany(N+1)` instead of `SELECT * FROM (sql) LIMIT N`.

## Version discipline

The platform version lives in exactly two places — bump BOTH together
whenever a user-visible change lands (minor for features, patch for fixes):

1. `backend/src/version.py` → `PLATFORM_VERSION`
2. `frontend/package.json` → `"version"` (then rebuild `frontend/dist` if
   users browse the built SPA on :8800)

The sidebar shows `v{UI} · API v{backend}` and flags a mismatch — that's the
"am I looking at a stale bundle/backend?" indicator. Don't add version
literals anywhere else; import `PLATFORM_VERSION` / use `__APP_VERSION__`.

## Don'ts

- Don't claim a feature is done if `green-gate.py` is red.
- Don't mark a UI feature complete without exercising the actual user click path — at minimum, a `TestClient` integration test of the backend route.
- Don't put committed config in `.claude/settings.local.json` (it's gitignored). Project-wide config goes in `.claude/settings.json`.
