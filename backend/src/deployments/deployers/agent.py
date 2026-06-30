import json
import logging
from pathlib import Path

import httpx

from ...config import settings
from .base import Deployer, HealthResult, TargetInfo

logger = logging.getLogger(__name__)


class AgentDeployer(Deployer):
    """Talks to an aihub-agent over HTTP using a shared bearer token."""

    @property
    def base_url(self) -> str:
        scheme = self.target.extra_config.get("scheme", "http") if isinstance(self.target.extra_config, dict) else "http"
        return f"{scheme}://{self.target.host}:{self.target.port}"

    @property
    def public_host(self) -> str:
        if isinstance(self.target.extra_config, dict):
            override = self.target.extra_config.get("public_host")
            if override:
                return override
        return self.target.host

    def _client(self) -> httpx.AsyncClient:
        if not self.credential_value:
            raise RuntimeError(
                "No agent token linked. Edit this target and pick a Secret with "
                "category 'agent_token' in the Credential field. Create one in "
                "Admin → Secrets if you haven't yet (the start.bat default is 'aihub-dev-token')."
            )
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.credential_value}"},
            timeout=settings.agent_request_timeout,
        )

    async def test_connection(self) -> TargetInfo:
        try:
            async with self._client() as client:
                resp = await client.get("/api/v1/info")
            if resp.status_code != 200:
                return TargetInfo(ok=False, detail=f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            return TargetInfo(
                ok=True,
                agent_version=data.get("agent_version"),
                ports_used=data.get("ports_used"),
                ports_total=data.get("ports_total"),
            )
        except httpx.HTTPError as e:
            return TargetInfo(ok=False, detail=f"{type(e).__name__}: {e}")
        except RuntimeError as e:
            return TargetInfo(ok=False, detail=str(e))

    async def deploy(self, deployment, artifact_tar: Path, port: int) -> str:
        meta = json.dumps({"version": deployment.version, "port": port})
        async with self._client() as client:
            with artifact_tar.open("rb") as fh:
                resp = await client.post(
                    f"/api/v1/apps/{deployment.app_id}/deploy",
                    data={"meta": meta},
                    files={"tarball": (artifact_tar.name, fh, "application/gzip")},
                    timeout=httpx.Timeout(settings.agent_request_timeout * 4),
                )
        if resp.status_code >= 400:
            raise RuntimeError(f"Agent deploy failed: HTTP {resp.status_code} — {resp.text[:300]}")
        data = resp.json()
        url = data.get("public_url")
        if not url:
            raise RuntimeError("Agent did not return a public_url")
        # Replace agent-reported host with our configured public host (handles 0.0.0.0 / localhost cases)
        if self.public_host and "localhost" in url:
            url = url.replace("localhost", self.public_host)
        return url

    async def stop(self, deployment) -> None:
        async with self._client() as client:
            resp = await client.post(f"/api/v1/apps/{deployment.app_id}/stop")
        if resp.status_code not in (200, 404):
            raise RuntimeError(f"Agent stop failed: HTTP {resp.status_code} — {resp.text[:200]}")

    async def health(self, deployment) -> HealthResult:
        try:
            async with self._client() as client:
                resp = await client.get(f"/api/v1/apps/{deployment.app_id}/health")
            if resp.status_code == 404:
                return HealthResult(ok=False, detail="Not deployed on agent")
            if resp.status_code != 200:
                return HealthResult(ok=False, detail=f"HTTP {resp.status_code}")
            data = resp.json()
            return HealthResult(
                ok=bool(data.get("last_probe_ok")) and data.get("status") == "running",
                detail=data.get("status", ""),
            )
        except httpx.HTTPError as e:
            return HealthResult(ok=False, detail=str(e))

    async def tail_logs(self, deployment, n: int = 200) -> list[str]:
        try:
            async with self._client() as client:
                resp = await client.get(f"/api/v1/apps/{deployment.app_id}/logs", params={"n": n})
            if resp.status_code != 200:
                return [f"[error fetching logs: HTTP {resp.status_code}]"]
            return resp.json().get("lines", [])
        except httpx.HTTPError as e:
            return [f"[error fetching logs: {e}]"]
