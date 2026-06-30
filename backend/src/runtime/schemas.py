from pydantic import BaseModel


class AppStartRequest(BaseModel):
    source: str = "draft"  # "draft" or "v{N}"


class AppStatusResponse(BaseModel):
    app_id: str
    status: str  # starting, running, stopped, error
    port: int | None = None
    source: str | None = None
    error: str | None = None
    # Live progress while status='starting'. Drives the "Installing
    # dependencies (15s)..." line in the UI's loading spinner.
    phase: str | None = None
    # queued | installing | spawning | waiting | running | failed | stopped
    phase_detail: str | None = None
    phase_elapsed_seconds: float | None = None  # since the current phase began
