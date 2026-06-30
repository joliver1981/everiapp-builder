from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TargetInfo:
    ok: bool
    detail: str = ""
    agent_version: str | None = None
    ports_used: list[int] | None = None
    ports_total: int | None = None


@dataclass
class HealthResult:
    ok: bool
    detail: str = ""


class Deployer(ABC):
    """Strategy interface — one implementation per DeploymentTarget.kind."""

    def __init__(self, target, credential_value: str | None):
        self.target = target
        self.credential_value = credential_value

    @abstractmethod
    async def test_connection(self) -> TargetInfo:
        ...

    @abstractmethod
    async def deploy(self, deployment, artifact_tar: Path, port: int) -> str:
        """Push the artifact, start the app, return the public URL."""

    @abstractmethod
    async def stop(self, deployment) -> None:
        ...

    @abstractmethod
    async def health(self, deployment) -> HealthResult:
        ...

    @abstractmethod
    async def tail_logs(self, deployment, n: int = 200) -> list[str]:
        ...
