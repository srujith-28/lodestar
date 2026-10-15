"""Validated API models for agent tasks, traces, tools, and reusable skills."""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.memory_schemas import contains_credential


TaskStatus = Literal["pending", "running", "completed", "failed", "awaiting_approval"]
SkillStatus = Literal["draft", "pending_approval", "approved", "disabled"]


class AgentTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_input: str = Field(min_length=1, max_length=4_000)
    tool_name: str | None = Field(default=None, min_length=1, max_length=80)
    tool_input: dict[str, Any] = Field(default_factory=dict)
    skill_id: int | None = Field(default=None, gt=0)

    @field_validator("task_input", "tool_name", mode="before")
    @classmethod
    def trim_inputs(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("task_input")
    @classmethod
    def reject_secret_like_task_input(cls, value: str) -> str:
        if not value:
            raise ValueError("Task input cannot be blank.")
        if contains_credential(value):
            raise ValueError("Credential-like task input cannot be stored.")
        return value

    @model_validator(mode="after")
    def require_one_execution_target(self) -> "AgentTaskCreate":
        if (self.tool_name is None) == (self.skill_id is None):
            raise ValueError("Choose exactly one registered tool or approved skill.")
        if self.skill_id is not None and self.tool_input:
            raise ValueError("Tool input is supplied by the selected skill.")
        return self


class AgentTaskApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool


class AgentTaskRerun(BaseModel):
    """Explicit confirmation required before replaying a write-capable task."""

    model_config = ConfigDict(extra="forbid")

    confirm_write: bool = False


class AgentTaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    skill_id: int | None
    task_input: str
    status: TaskStatus
    plan_steps: list[dict[str, Any]]
    result_summary: str | None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class AgentTraceEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    step_index: int | None
    event_type: str
    tool_name: str | None
    status: str
    approval_required: bool
    input_data: dict[str, Any] | None
    result_data: Any | None
    message: str | None
    created_at: datetime

    @field_validator("created_at", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class AgentTaskTraceResponse(BaseModel):
    task: AgentTaskResponse
    events: list[AgentTraceEventResponse]


class SkillStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=200)
    tool_input: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool_name", "description", mode="before")
    @classmethod
    def trim_step_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("description")
    @classmethod
    def reject_secret_like_description(cls, value: str) -> str:
        if not value:
            raise ValueError("Step description cannot be blank.")
        if contains_credential(value):
            raise ValueError("Credential-like text cannot be stored in a skill.")
        return value


class SkillCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    steps: list[SkillStep] = Field(min_length=1, max_length=10)

    @field_validator("name", "description", mode="before")
    @classmethod
    def trim_skill_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("name", "description")
    @classmethod
    def reject_secret_like_skill_text(cls, value: str) -> str:
        if not value:
            raise ValueError("This field cannot be blank.")
        if contains_credential(value):
            raise ValueError("Credential-like text cannot be stored in a skill.")
        return value


class SkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    steps: list[SkillStep]
    status: SkillStatus
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalize_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class ToolCatalogItem(BaseModel):
    name: str
    description: str
    permission_level: Literal["read", "write"]
    requires_approval: bool
    input_schema: dict[str, Any]
