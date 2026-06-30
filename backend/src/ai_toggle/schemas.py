from pydantic import BaseModel


class DataSourceContext(BaseModel):
    name: str
    columns: list[str] = []
    description: str = ""
    rowCount: int = 0
    sampleRows: list[dict] = []


class ActionContext(BaseModel):
    name: str
    description: str = ""
    params: list[str] = []


class ToggleChatContext(BaseModel):
    dataSources: list[DataSourceContext] = []
    availableActions: list[str] = []


class ToggleChatRequest(BaseModel):
    message: str
    context: ToggleChatContext = ToggleChatContext()


class ActionCommand(BaseModel):
    name: str
    params: dict = {}


class ToggleChatResponse(BaseModel):
    response: str
    actions: list[ActionCommand] = []
