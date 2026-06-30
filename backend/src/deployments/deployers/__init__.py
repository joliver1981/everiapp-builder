from .agent import AgentDeployer
from .base import Deployer, HealthResult, TargetInfo
from .ssh import SshDeployer

DEPLOYERS: dict[str, type[Deployer]] = {
    "agent": AgentDeployer,
    "ssh": SshDeployer,
}


def get_deployer(target, credential_value: str | None) -> Deployer:
    cls = DEPLOYERS.get(target.kind)
    if cls is None:
        raise ValueError(f"Unknown deployer kind: {target.kind}")
    return cls(target, credential_value)


__all__ = ["Deployer", "HealthResult", "TargetInfo", "DEPLOYERS", "get_deployer"]
