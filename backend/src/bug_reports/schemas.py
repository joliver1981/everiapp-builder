from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------- Intake (called from deployed apps) ----------

class CapturedNetworkError(BaseModel):
    url: str
    status: int | None = None
    method: str | None = None
    error: str | None = None
    timestamp: float | None = None


class CapturedContext(BaseModel):
    page_url: str = ""
    user_agent: str = ""
    viewport: dict[str, int] | None = None
    console_tail: list[str] = Field(default_factory=list)
    network_errors: list[CapturedNetworkError] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)


class BugReportIntake(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    description: str = ""
    version: int | None = None
    deployment_id: str | None = None
    reporter_label: str | None = None  # name/email if anon (no auth)
    captured_context: CapturedContext | None = None
    screenshot_data_url: str | None = None  # data:image/png;base64,...


# ---------- Responses ----------

class ProposedFile(BaseModel):
    path: str
    action: Literal["create", "update", "delete"]
    content: str = ""
    current_content: str | None = None


class BugAnalysisResponse(BaseModel):
    id: str
    bug_report_id: str
    diagnosis: str
    root_cause: str
    proposed_files: list[ProposedFile]
    risk_level: Literal["low", "medium", "high"]
    risk_rationale: str
    llm_model: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FixAttemptResponse(BaseModel):
    id: str
    bug_report_id: str
    analysis_id: str
    base_version: int | None
    new_version: int | None
    deployment_id: str | None
    status: str
    auto_approved: bool
    approved_by: str | None
    approved_at: datetime | None
    error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BugReportResponse(BaseModel):
    id: str
    app_id: str
    version: int | None
    deployment_id: str | None
    reporter_user_id: str | None
    reporter_label: str | None
    title: str
    description: str
    captured_context: dict
    screenshot_url: str | None  # served back via /api/bug-reports/{id}/screenshot
    status: str
    error: str | None
    created_at: datetime
    updated_at: datetime
    analyses: list[BugAnalysisResponse] = Field(default_factory=list)
    attempts: list[FixAttemptResponse] = Field(default_factory=list)


class BugReportSummary(BaseModel):
    """Lightweight row for the list view (no captured context, no analyses)."""
    id: str
    app_id: str
    app_name: str | None = None
    version: int | None
    title: str
    status: str
    risk_level: str | None  # latest analysis risk
    auto_approve_enabled: bool = False  # convenience flag for UI sort/filter
    reporter_label: str | None
    created_at: datetime


# ---------- Mutations from admin UI ----------

class ApprovalRequest(BaseModel):
    analysis_id: str | None = None  # if None, uses latest analysis
    notes: str = ""


class ReanalyzeRequest(BaseModel):
    note: str = ""  # optional context to add ("focus on the network error")
