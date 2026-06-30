# AIHub platform — multi-stage build.
#   stage 1: build the React frontend → static assets
#   stage 2: python runtime with the backend + the built frontend served by it
#
# Result: one image that serves the API on :8800 and the SPA from the same
# origin. The agent is installed separately on each target host.

# ---- stage 1: frontend build ---------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
# Build the SPA. VITE_AIHUB_BASE_URL empty = same-origin (the backend serves it).
RUN npm run build

# ---- stage 2: backend runtime --------------------------------------------
FROM python:3.12-slim AS runtime

# System deps: unixodbc for MSSQL driver support (optional dialects), plus
# build tools for any wheels that need compiling.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ unixodbc unixodbc-dev curl gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the backend package + its deps
COPY backend/pyproject.toml backend/
COPY backend/src backend/src
RUN pip install --no-cache-dir -e backend/

# Copy the built frontend so the backend can serve it
COPY --from=frontend /app/frontend/dist /app/frontend/dist

# Data dir (mounted as a volume in compose)
RUN mkdir -p /data /data/apps /data/archives
ENV DATABASE_URL="sqlite+aiosqlite:////data/aihub.db" \
    APP_DATA_DIR="/data/apps" \
    AUDIT_ARCHIVE_DIR="/data/archives" \
    HOST="0.0.0.0" \
    PORT="8800"

EXPOSE 8800

# uvicorn serves the FastAPI app (which also mounts the static SPA).
# --proxy-headers + --forwarded-allow-ips=* so X-Forwarded-Proto/-For from the
# front TLS proxy are honored (correct request scheme → auto Secure cookies, and
# real client IPs in access logs). Safe here: only the proxy fronts the container.
CMD ["python", "-m", "uvicorn", "src.main:app", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8800", "--proxy-headers", "--forwarded-allow-ips", "*"]
