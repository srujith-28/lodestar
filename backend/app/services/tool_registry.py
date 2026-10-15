"""Explicit, typed allowlist of local tools available to agent tasks and skills."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from app.api.memory_schemas import MemoryCreate, MemoryResponse, contains_credential
from app.models import Memory
from app.services.memory_search import search_memory_rows


class MemorySearchToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("query", mode="before")
    @classmethod
    def trim_query(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("query")
    @classmethod
    def reject_secret_like_query(cls, value: str) -> str:
        if not value:
            raise ValueError("Search query cannot be blank.")
        if contains_credential(value):
            raise ValueError("Credential-like search input is not allowed.")
        return value


class CurrentDateTimeToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    permission_level: str
    requires_approval: bool
    input_model: Type[BaseModel]
    execute: Callable[[Session, BaseModel], Any]


class UnknownToolError(ValueError):
    """Raised when a requested tool is not explicitly registered."""


class InvalidToolInputError(ValueError):
    """Raised when a registered tool input does not match its schema."""


def _search_memories(db: Session, payload: BaseModel) -> dict[str, Any]:
    request = MemorySearchToolInput.model_validate(payload.model_dump())
    matches = search_memory_rows(db, request.query, request.limit)
    return {
        "count": len(matches),
        "matches": [
            MemoryResponse.model_validate(memory).model_dump(mode="json")
            for memory in matches
        ],
    }


def _current_datetime(_: Session, __: BaseModel) -> dict[str, str]:
    return {"utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


def _save_memory(db: Session, payload: BaseModel) -> dict[str, Any]:
    request = MemoryCreate.model_validate(payload.model_dump())
    memory = Memory(**request.model_dump())
    try:
        db.add(memory)
        db.commit()
        db.refresh(memory)
    except Exception:
        db.rollback()
        raise
    return MemoryResponse.model_validate(memory).model_dump(mode="json")


TOOL_REGISTRY: dict[str, ToolDefinition] = {
    "memory_search": ToolDefinition(
        name="memory_search",
        description="Search saved local memories by keyword. Read-only.",
        permission_level="read",
        requires_approval=False,
        input_model=MemorySearchToolInput,
        execute=_search_memories,
    ),
    "current_datetime": ToolDefinition(
        name="current_datetime",
        description="Read the current UTC date and time. Read-only.",
        permission_level="read",
        requires_approval=False,
        input_model=CurrentDateTimeToolInput,
        execute=_current_datetime,
    ),
    "save_memory": ToolDefinition(
        name="save_memory",
        description="Save a user-approved memory in the local database.",
        permission_level="write",
        requires_approval=True,
        input_model=MemoryCreate,
        execute=_save_memory,
    ),
}


def get_tool(name: str) -> ToolDefinition:
    try:
        return TOOL_REGISTRY[name]
    except KeyError as exc:
        raise UnknownToolError(f"Tool '{name}' is not registered.") from exc


def validate_tool_input(name: str, input_data: dict[str, Any]) -> tuple[ToolDefinition, BaseModel]:
    definition = get_tool(name)
    try:
        validated = definition.input_model.model_validate(input_data)
    except ValidationError as exc:
        raise InvalidToolInputError("Tool input does not match its registered schema.") from exc
    return definition, validated


def describe_tools() -> list[dict[str, Any]]:
    """Return the explicit registry metadata for the UI and API clients."""
    return [
        {
            "name": definition.name,
            "description": definition.description,
            "permission_level": definition.permission_level,
            "requires_approval": definition.requires_approval,
            "input_schema": definition.input_model.model_json_schema(),
        }
        for definition in TOOL_REGISTRY.values()
    ]
