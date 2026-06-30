import json
import tarfile
from pathlib import Path

import httpx
import pytest

from src.deployments.deployers.agent import AgentDeployer
from src.deployments.models import Deployment, DeploymentTarget


def _target():
    return DeploymentTarget(
        id="t1", name="local", kind="agent", host="localhost", port=8765,
        port_range_start=9100, port_range_end=9120, environment="dev",
        is_active=True, extra_config={},
    )


def _deployment(port=9100):
    return Deployment(
        id="d1", app_id="app-x", version=4, target_id="t1",
        allocated_port=port, status="building", deployed_by="u1",
    )


@pytest.mark.asyncio
async def test_deploy_posts_tarball_with_meta(tmp_path: Path, monkeypatch):
    # Build a minimal tarball
    tar = tmp_path / "v4.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        f = tmp_path / "index.html"
        f.write_bytes(b"hi")
        tf.add(f, arcname="dist/index.html")

    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["content_type"] = request.headers.get("content-type")
        captured["body"] = request.content
        return httpx.Response(200, json={"public_url": "http://localhost:9100"})

    transport = httpx.MockTransport(handler)

    deployer = AgentDeployer(_target(), credential_value="bearer-secret")

    # Patch _client() to use our mock transport
    def fake_client(self):
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.credential_value}"},
            transport=transport,
        )

    monkeypatch.setattr(AgentDeployer, "_client", fake_client)

    url = await deployer.deploy(_deployment(), tar, port=9100)
    assert url == "http://localhost:9100"
    assert captured["url"].endswith("/api/v1/apps/app-x/deploy")
    assert captured["auth"] == "Bearer bearer-secret"
    assert captured["content_type"].startswith("multipart/form-data")
    # Verify the meta JSON we sent
    body = captured["body"].decode("latin-1", errors="replace")
    assert '"version": 4' in body
    assert '"port": 9100' in body


@pytest.mark.asyncio
async def test_deploy_raises_on_agent_error(tmp_path: Path, monkeypatch):
    tar = tmp_path / "v4.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        f = tmp_path / "index.html"
        f.write_bytes(b"hi")
        tf.add(f, arcname="dist/index.html")

    async def handler(request):
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    deployer = AgentDeployer(_target(), credential_value="x")

    def fake_client(self):
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.credential_value}"},
            transport=transport,
        )

    monkeypatch.setattr(AgentDeployer, "_client", fake_client)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        await deployer.deploy(_deployment(), tar, port=9100)


@pytest.mark.asyncio
async def test_test_connection_reads_info(monkeypatch):
    async def handler(request):
        assert request.url.path.endswith("/api/v1/info")
        return httpx.Response(200, json={
            "agent_version": "9.9.9",
            "ports_used": [9100],
            "ports_total": 100,
        })

    transport = httpx.MockTransport(handler)
    deployer = AgentDeployer(_target(), credential_value="x")

    def fake_client(self):
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.credential_value}"},
            transport=transport,
        )

    monkeypatch.setattr(AgentDeployer, "_client", fake_client)

    info = await deployer.test_connection()
    assert info.ok
    assert info.agent_version == "9.9.9"
    assert info.ports_total == 100
    assert info.ports_used == [9100]
