"""Versioned HTTP contract between the application server and Agent service."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, SecretStr


class ToolContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    parameters: dict[str, Any]
    required: list[str] = []


class SkillContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    instructions: str
    tools: list[ToolContract] = []


class ModelConfigContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    model: str
    api_key: SecretStr
    request_timeout: float = 90
    max_requests: int = 12
    max_tool_calls: int = 20
    run_timeout: float = 300
    max_retries: int = 2
    session_id: str = ""
    disable_thinking: bool = False


class ExecutionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_id: str
    task_name: str
    prompt: str
    materials: dict[str, Any]
    submit: ToolContract | None = None
    tools: list[ToolContract] = []
    skills: list[SkillContract] = []
    allowed_skills: list[str] = []
    request_limit: int | None = None
    max_requests: int
    max_tool_calls: int
    timeout_seconds: float
    requests_used: int = 0
    tool_calls_used: int = 0
    stream: bool = False
    tool_limit_action: Literal["fail", "finish"] = "fail"
    callback_url: str
    session_key: str | None = None
    config: ModelConfigContract


class ExecutionReceipt(BaseModel):
    execution_id: str
    status: Literal["queued"]


class ExecutionSnapshot(BaseModel):
    execution_id: str
    status: Literal["queued", "running", "succeeded", "failed", "canceled", "interrupted"]
    result: Any = None
    error: str | None = None
    requests: int = 0
    tool_calls: int = 0
    mounted_tools: list[str] = []


class ExecutionEvent(BaseModel):
    id: int
    stage: str
    message: str = ""
    data: dict[str, Any] | None = None
