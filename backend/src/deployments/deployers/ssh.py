import logging
import shlex
from pathlib import Path

from ...config import settings
from .base import Deployer, HealthResult, TargetInfo

logger = logging.getLogger(__name__)


def _remote_app_dir(app_id: str, version: int) -> str:
    return f"~/aihub-apps/{app_id}/v{version}"


class SshDeployer(Deployer):
    """Pushes the artifact via SFTP and runs `npx serve` over SSH.

    Assumes the target host has node + npx available. The remote directory
    layout is ~/aihub-apps/{app_id}/v{N}/dist (unpacked) plus app.pid + app.log.
    """

    @property
    def public_host(self) -> str:
        if isinstance(self.target.extra_config, dict):
            override = self.target.extra_config.get("public_host")
            if override:
                return override
        return self.target.host

    def _connect(self):
        import asyncssh  # local import — optional dep

        if not self.credential_value:
            raise RuntimeError("SSH target has no credential (private key) configured")

        return asyncssh.connect(
            host=self.target.host,
            port=self.target.port,
            username=self.target.ssh_user or "root",
            client_keys=[asyncssh.import_private_key(self.credential_value)],
            known_hosts=None,
            connect_timeout=settings.ssh_connect_timeout,
        )

    async def test_connection(self) -> TargetInfo:
        try:
            async with self._connect() as conn:
                result = await conn.run("uname -a || ver", check=False)
            detail = (result.stdout or result.stderr or "").strip().splitlines()[0:1]
            return TargetInfo(ok=True, detail=detail[0] if detail else "ok")
        except ImportError:
            return TargetInfo(ok=False, detail="asyncssh is not installed in the backend env")
        except Exception as e:
            return TargetInfo(ok=False, detail=f"{type(e).__name__}: {e}")

    async def deploy(self, deployment, artifact_tar: Path, port: int) -> str:
        remote_dir = _remote_app_dir(deployment.app_id, deployment.version)
        remote_tar = f"{remote_dir}.tar.gz"
        try:
            async with self._connect() as conn:
                await conn.run(f"mkdir -p {shlex.quote(remote_dir)}", check=True)

                async with conn.start_sftp_client() as sftp:
                    await sftp.put(str(artifact_tar), remote_tar)

                # Unpack, then start `npx serve` detached, write PID.
                start_cmd = (
                    f"cd {shlex.quote(remote_dir)} && "
                    f"tar -xzf ../v{deployment.version}.tar.gz && "
                    f"rm -f ../v{deployment.version}.tar.gz && "
                    # Kill any prior process for this version, ignore if absent
                    f"if [ -f app.pid ]; then kill $(cat app.pid) 2>/dev/null || true; fi && "
                    f"nohup npx --yes serve -s dist -l {port} "
                    f"  > app.log 2>&1 < /dev/null & echo $! > app.pid && "
                    f"sleep 2 && curl -fsS -o /dev/null http://127.0.0.1:{port}/ "
                )
                # We allow the curl probe to fail (slow boots happen) but report stderr if so.
                result = await conn.run(start_cmd, check=False)
                if result.exit_status not in (0, 22, 28, 7):
                    # Non-curl-related failure
                    raise RuntimeError(
                        f"Remote start failed (exit {result.exit_status}): "
                        f"{(result.stderr or result.stdout or '')[:300]}"
                    )
        except ImportError:
            raise RuntimeError("asyncssh is not installed in the backend env")

        return f"http://{self.public_host}:{port}"

    async def stop(self, deployment) -> None:
        remote_dir = _remote_app_dir(deployment.app_id, deployment.version)
        cmd = (
            f"if [ -f {remote_dir}/app.pid ]; then "
            f"  kill $(cat {remote_dir}/app.pid) 2>/dev/null || true; "
            f"  rm -f {remote_dir}/app.pid; "
            f"fi"
        )
        async with self._connect() as conn:
            await conn.run(cmd, check=False)

    async def health(self, deployment) -> HealthResult:
        if not deployment.allocated_port:
            return HealthResult(ok=False, detail="No port allocated")
        try:
            async with self._connect() as conn:
                result = await conn.run(
                    f"curl -fsS -o /dev/null -w '%{{http_code}}' "
                    f"http://127.0.0.1:{deployment.allocated_port}/",
                    check=False,
                )
            code = (result.stdout or "").strip()
            return HealthResult(ok=code.startswith("2"), detail=f"HTTP {code or 'unreachable'}")
        except Exception as e:
            return HealthResult(ok=False, detail=str(e))

    async def tail_logs(self, deployment, n: int = 200) -> list[str]:
        remote_dir = _remote_app_dir(deployment.app_id, deployment.version)
        try:
            async with self._connect() as conn:
                result = await conn.run(
                    f"tail -n {int(n)} {remote_dir}/app.log 2>/dev/null || true",
                    check=False,
                )
            return (result.stdout or "").splitlines()
        except Exception as e:
            return [f"[error fetching logs: {e}]"]
