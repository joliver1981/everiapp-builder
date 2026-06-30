# AIHub

AIHub is a self-hostable platform for building, deploying, and operating
AI-generated web apps. Describe an app in natural language and AIHub generates a
React + TypeScript application, verifies it (type-check → build → boot →
runtime), versions it immutably, and deploys it to your own infrastructure.

It is designed for **on-premise / single-tenant** use: your data, your models,
your servers. SQLite is the default datastore for both the control plane and
generated apps; you can also connect your own centralized databases.

> **Status:** early release (0.1.0). APIs and storage formats may change.

## Features

- **AI app builder** — conversational generation of full React/TypeScript apps
  from a prompt, with an iterative self-heal loop.
- **Multi-provider LLMs** via [LiteLLM](https://github.com/BerriAI/litellm) —
  Anthropic, OpenAI, and others, configured in the admin UI.
- **Verification pipeline** — generated apps are type-checked, built, booted,
  and (optionally) runtime- and accessibility-probed before they ship.
- **Immutable versioning**, blue/green deploys, health checks, and auto-rollback.
- **Data layer** — platform and per-app SQLite stores, plus external
  Connections/Datasets (PostgreSQL, MySQL, SQL Server, Oracle) with PII
  tagging/redaction, query caching, and lineage.
- **Enterprise auth** — local accounts, LDAP/Active Directory, SAML, and OIDC
  single sign-on.
- **Governance** — secrets management (Fernet-encrypted), audit logging, SIEM
  forwarding, static security scanning, dependency scanning, and a publish
  approval workflow.

## Repository layout

| Path           | What it is                                                    |
| -------------- | ------------------------------------------------------------- |
| `backend/`     | FastAPI control plane (`backend/src/`), pytest in `backend/tests/` |
| `frontend/`    | React 19 + Vite + Tailwind v4 + shadcn/ui admin & builder SPA |
| `aihub-agent/` | Python deployment daemon installed on target hosts            |
| `app-template/`| Scaffold cloned for each AI-generated app                     |
| `app-sdk/`     | `@aihub/app-sdk` package the generated apps consume           |

## Prerequisites

- Python 3.12+
- Node.js 20+
- (Optional) ODBC / database client libraries for non-SQLite Connections —
  see [External database drivers](#external-database-drivers).

## Quick start (development)

On Windows, `start.bat` provisions a local virtual environment, installs backend
and frontend dependencies on first run, and launches all three services:

```bat
copy .env.example .env   :: then edit .env (see Configuration)
start.bat
```

This starts:

- Backend API on <http://localhost:8800>
- Frontend dev server on <http://localhost:5173>
- Deployment agent on <http://localhost:8765>

On first launch the platform database is created automatically and a first-run
setup wizard walks you through creating the initial admin account. **No database
file is shipped** — the schema is built from the SQLAlchemy models on first boot.

For other platforms, run the backend (`uvicorn src.main:app` from `backend/`),
the agent, and the frontend (`npm run dev`) individually; `start.bat` is a
convenience wrapper around the same steps.

## Configuration

Copy `.env.example` to `.env` and set at least the following before running in
anything other than local development:

| Variable                | Purpose                                             |
| ----------------------- | --------------------------------------------------- |
| `JWT_SECRET_KEY`        | Signs access tokens — **must** be a strong random value in production (startup fails if left at the default when `DEBUG=false`). |
| `MASTER_ENCRYPTION_KEY` | Fernet key used to encrypt stored secrets. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Keep it stable, or stored secrets become unreadable. |
| `DATABASE_URL`          | Defaults to a local SQLite file under `data/`.      |

The platform database lives under `data/` (git-ignored). The directory is kept
in the repo via `data/.gitkeep`; the `.db` file itself is created on first run.

### External database drivers

SQLite is bundled. Other dialects are opt-in extras declared in
`backend/pyproject.toml`:

| Dialect    | Install                          |
| ---------- | -------------------------------- |
| PostgreSQL | `pip install -e .[postgres]`     |
| MySQL      | `pip install -e .[mysql]`        |
| SQL Server | `pip install -e .[mssql]` (also needs the OS-level ODBC Driver 17/18) |
| Oracle     | `pip install -e .[oracle]`       |
| All        | `pip install -e .[all-dbs]`      |

## Tests

```bash
cd backend && pytest -q          # backend
cd aihub-agent && pytest -q      # deployment agent
cd frontend && npx tsc --noEmit  # frontend type-check
```

Some integration tests are opt-in and skip automatically when their backing
service (e.g. a SQL Server instance) is unavailable.

## Security

Please report security vulnerabilities responsibly — see
[`SECURITY.md`](SECURITY.md) if present, or open a private report rather than a
public issue. Never commit real secrets; use `.env` (git-ignored) and the
built-in secrets manager.

## Project notes

- The `backend/src/licensing/` module is **dormant scaffolding** for a possible
  future commercial tier. It is not wired into any enforcement and gates no
  functionality today.

## License

Licensed under the [Apache License 2.0](LICENSE). See [`NOTICE`](NOTICE) for
attribution and third-party components.
