"""Validated request and response models for user-controlled memories."""

import re
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:[A-Z0-9]+[_-])*(?:api[_\s-]?key|access[_\s-]?token|refresh[_\s-]?token|client[_\s-]?secret|"
        r"password|passwd|secret|credential)\b(?:\s*[:=]\s*|\s+is\s+)['\"]?[^\s,'\";]+",
        re.IGNORECASE,
    ),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.IGNORECASE),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35})\b"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@[^/\s]+", re.IGNORECASE),
)


def contains_credential(value: str) -> bool:
    """Recognize common credential formats before content reaches storage."""
    return any(pattern.search(value) for pattern in SECRET_PATTERNS)


class MemoryFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=5_000)
    category: str = Field(default="general", min_length=1, max_length=60)
    source: str = Field(default="user", min_length=1, max_length=80)

    @field_validator("content", "category", "source", mode="before")
    @classmethod
    def trim_memory_fields(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("content", "category", "source")
    @classmethod
    def reject_blank_or_credential_values(cls, value: str) -> str:
        if not value:
            raise ValueError("This field cannot be blank.")
        if contains_credential(value):
            raise ValueError("Credentials and secrets cannot be saved as memories.")
        return value


class MemoryCreate(MemoryFields):
    """Fields accepted when creating a memory."""


class MemoryUpdate(MemoryFields):
    """Fields accepted when replacing a memory."""


class MemorySearchRequest(BaseModel):
    """Private keyword search parameters sent in a request body."""

    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=50, ge=1, le=200)

    @field_validator("q", mode="before")
    @classmethod
    def trim_query(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("q")
    @classmethod
    def validate_query_terms(cls, value: str) -> str:
        if not value:
            raise ValueError("Search query cannot be blank.")
        if len(value.split()) > 10:
            raise ValueError("Search accepts at most 10 keywords.")
        return value


class MemoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    content: str
    category: str
    source: str
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def normalize_timestamp_to_utc(cls, value: datetime) -> datetime:
        # SQLite stores timezone-aware DateTime values without the tzinfo marker.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
