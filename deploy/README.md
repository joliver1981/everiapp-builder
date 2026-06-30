# AIHub on-prem deployment

Three ways to install the **control plane** (the platform = backend + UI).
The **agent** is a separate per-host install (see `aihub-agent/installer/`).

| Method | Best for | How |
|---|---|---|
| Docker Compose | any OS with Docker; cleanest upgrades | `deploy/docker-compose.yml` |
| Windows installer | Windows Server shops without Docker | `aihub-agent/installer/` pattern (platform installer: roadmap) |
| Linux .deb/.rpm | RHEL/Ubuntu prod | roadmap |

## Docker Compose (recommended)

```bash
cd deploy
cp .env.example .env
# edit .env — set JWT_SECRET_KEY at minimum
docker compose up -d
# open http://localhost:8800   (admin / password in dev)
```

Data (SQLite DB, deployed app files, audit archives) lives in the `aihub-data`
named volume so upgrades are safe:

```bash
docker compose pull && docker compose up -d   # zero-config upgrade
docker compose exec aihub aihub doctor        # health check
docker compose exec aihub aihub backup --to /data/backups   # online backup
```

## Hardware sizing

| Concurrent users | CPU | RAM | Disk | DB |
|---|---|---|---|---|
| ≤ 50 | 4 vCPU | 8 GB | 50 GB | tuned SQLite (default) |
| 50–150 | 8 vCPU | 16 GB | 100 GB | tuned SQLite + audit rotation |
| 150–500 | 16 vCPU | 32 GB | 250 GB | consider Postgres via DATABASE_URL |
| > 500 | — | — | — | Postgres + HA (roadmap) |

Network: the platform listens on TCP 8800. Each agent listens on TCP 8765 on
its own host; the platform must be able to reach `agent_host:8765`.

## Operator CLI (inside the container or the venv)

```
aihub doctor          # pragmas, indexes, disk, audit health
aihub backup --to DIR # online backup tarball
aihub rotate-audit    # archive old audit rows to JSONL.gz
aihub upgrade         # run migrations after a version bump (backs up first)
aihub license show    # current license
aihub license install FILE
```

## Config file (mass deployment)

For SCCM / Ansible / Group Policy, drop a YAML config at one of:
  - `$AIHUB_CONFIG_FILE`
  - `%ProgramData%\AIHub\config.yaml` (Windows)
  - `/etc/aihub/aihub.yaml` (Linux)

Keys map to the same names as the env vars (lowercase). Precedence:
**env var > .env > YAML config > built-in default**.

```yaml
# /etc/aihub/aihub.yaml
jwt_access_token_expire_minutes: 30
audit_retention_months: 36
ad_mode: ldap
```
