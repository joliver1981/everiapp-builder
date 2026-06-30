from pydantic import BaseModel


class ChatRequest(BaseModel):
    app_id: str
    message: str
    conversation_id: str | None = None


class GeneratedFile(BaseModel):
    path: str
    content: str
    action: str = "create"  # create, modify, delete
