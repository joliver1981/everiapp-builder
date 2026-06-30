import asyncio
import json
import logging
import platform
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import JSONResponse

from . import __version__
from .apps import registry
from .auth import require_token
from .config import settings
from .filestore import remove_app_dir, write_artifact
from .health import health_loop
from .ports import is_port_free, port_pool

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.apps_dir.mkdir(parents=True, exist_ok=True)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    if not settings.agent_token:
        logger.warning("AGENT_TOKEN is empty — all requests will be rejected.")
    health_task = asyncio.create_task(health_loop())
    logger.info("aihub-agent %s started on %s:%d", __version__, settings.agent_host, settings.agent_port)
    try:
        yield
    finally:
        health_task.cancel()
        await registry.stop_all()


app = FastAPI(title="aihub-agent", version=__version__, lifespan=lifespan)


def _public_url(request: Request, port: int) -> str:
    if settings.public_host_override:
        host = settings.public_host_override
    else:
        host = request.url.hostname or "localhost"
    return f"http://{host}:{port}"


def _serialize(app_obj) -> dict:
    return {
        "app_id": app_obj.app_id,
        "version": app_obj.version,
        "port": app_obj.port,
        "status": app_obj.status,
        "error": app_obj.error,
        "started_at": app_obj.started_at.isoformat(),
        "last_probe_at": app_obj.last_probe_at.isoformat() if app_obj.last_probe_at else None,
        "last_probe_ok": app_obj.last_probe_ok,
    }


@app.get("/api/v1/info", dependencies=[Depends(require_token)])
async def info():
    return {
        "agent_version": __version__,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "port_range": [settings.app_port_range_start, settings.app_port_range_end],
        "ports_total": port_pool.total,
        "ports_used": port_pool.used,
    }


@app.get("/api/v1/apps", dependencies=[Depends(require_token)])
async def list_apps():
    return [_serialize(a) for a in registry.list()]


@app.post("/api/v1/apps/{app_id}/deploy", dependencies=[Depends(require_token)])
async def deploy(
    app_id: str,
    request: Request,
    meta: str = Form(..., description='JSON: {"version": int, "port": int|null}'),
    tarball: UploadFile = File(...),
):
    try:
        meta_obj = json.loads(meta)
        version = int(meta_obj["version"])
        preferred_port = meta_obj.get("port")
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid meta JSON: {e}")

    tar_bytes = await tarball.read()
    if not tar_bytes:
        raise HTTPException(status_code=400, detail="Empty tarball")

    try:
        port = await port_pool.allocate(preferred=preferred_port)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))

    if not is_port_free(port):
        await port_pool.release(port)
        raise HTTPException(
            status_code=409,
            detail=f"Port {port} is already bound by another process on this host",
        )

    try:
        serve_dir = write_artifact(app_id, version, tar_bytes)
    except Exception as e:
        await port_pool.release(port)
        raise HTTPException(status_code=400, detail=f"Failed to unpack artifact: {e}")

    deployed = await registry.deploy(app_id, version, port, serve_dir)
    if deployed.status != "running":
        return JSONResponse(
            status_code=500,
            content={**_serialize(deployed), "detail": deployed.error or "App failed to start"},
        )

    return {**_serialize(deployed), "public_url": _public_url(request, port)}


@app.post("/api/v1/apps/{app_id}/stop", dependencies=[Depends(require_token)])
async def stop(app_id: str):
    ok = await registry.stop(app_id)
    if not ok:
        raise HTTPException(status_code=404, detail="App not deployed on this agent")
    return {"stopped": True}


@app.delete("/api/v1/apps/{app_id}", dependencies=[Depends(require_token)])
async def remove(app_id: str):
    await registry.stop(app_id)
    remove_app_dir(app_id)
    return {"removed": True}


@app.get("/api/v1/apps/{app_id}/health", dependencies=[Depends(require_token)])
async def health(app_id: str):
    app_obj = registry.get(app_id)
    if not app_obj:
        raise HTTPException(status_code=404, detail="App not deployed on this agent")
    await registry.probe(app_obj)
    return _serialize(app_obj)


@app.get("/api/v1/apps/{app_id}/logs", dependencies=[Depends(require_token)])
async def logs(app_id: str, n: int = 200):
    app_obj = registry.get(app_id)
    if not app_obj or app_obj.log is None:
        raise HTTPException(status_code=404, detail="App not deployed on this agent")
    return {"lines": app_obj.log.tail(n)}
