import os
import sys
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings


def _default_data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return str(Path(base) / "aihub-agent")
    return "/var/lib/aihub-agent"


class AgentSettings(BaseSettings):
    agent_token: str = ""
    agent_host: str = "0.0.0.0"
    agent_port: int = 8765
    agent_data_dir: str = _default_data_dir()

    app_port_range_start: int = 9100
    app_port_range_end: int = 9199
    app_startup_timeout: int = 20

    public_host_override: str = ""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",  # tolerate AIHub backend's .env when run from same repo
    }

    # Defensive: Windows `set FOO=bar && ...` includes the trailing space before
    # `&&` in the env value, which made uvicorn fail with getaddrinfo on "0.0.0.0 ".
    # Strip whitespace from every string-typed env var we read.
    @field_validator("agent_token", "agent_host", "agent_data_dir", "public_host_override", mode="before")
    @classmethod
    def _strip_str(cls, v):
        if isinstance(v, str):
            return v.strip()
        return v

    @property
    def data_dir(self) -> Path:
        return Path(self.agent_data_dir)

    @property
    def apps_dir(self) -> Path:
        return self.data_dir / "apps"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"


settings = AgentSettings()
