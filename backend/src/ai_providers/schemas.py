from pydantic import BaseModel


class AIProviderCreate(BaseModel):
    name: str  # e.g., "OpenAI Production", "Anthropic"
    provider_type: str  # openai, anthropic, azure, google, ollama
    api_key: str
    base_url: str = ""  # For Azure/custom endpoints
    default_model: str = ""  # e.g., "gpt-5.5", "claude-opus-4-8", "gemini-3.1-pro-preview"
    is_default_generation: bool = False  # Default for app generation
    is_default_toggle: bool = False  # Default for AI Toggle
    extra_config: dict = {}  # Rate limits, org ID, etc.


class AIProviderUpdate(BaseModel):
    name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    default_model: str | None = None
    is_active: bool | None = None
    is_default_generation: bool | None = None
    is_default_toggle: bool | None = None
    extra_config: dict | None = None


class AIProviderResponse(BaseModel):
    id: str
    name: str
    provider_type: str
    is_active: bool
    is_default_generation: bool
    is_default_toggle: bool
    default_model: str
    base_url: str
    extra_config: dict
    last_verified: str | None
    created_at: str
    updated_at: str


class AIProviderTestResult(BaseModel):
    success: bool
    message: str
    model: str | None = None
    response_time_ms: int | None = None
