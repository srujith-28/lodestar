"""SQLAlchemy models for persistent user-owned data."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class Memory(Base):
    """A memory explicitly saved and controlled by the user."""

    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False, default="personal")
    is_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False, default="general")
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class AgentTask(Base):
    """A user-submitted task and its persisted local or model-proposed plan snapshot."""

    __tablename__ = "agent_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False, default="personal")
    is_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    skill_id: Mapped[int | None] = mapped_column(ForeignKey("skills.id"), nullable=True, index=True)
    task_input: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    plan_steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    trace_events: Mapped[list["AgentTraceEvent"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="AgentTraceEvent.id"
    )


class AgentTraceEvent(Base):
    """An append-only record of a task lifecycle or registered tool execution."""

    __tablename__ = "agent_trace_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_data: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    task: Mapped[AgentTask] = relationship(back_populates="trace_events")


class Skill(Base):
    """A reusable, user-authored sequence of allowlisted tool calls."""

    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False, default="personal")
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class AuthSession(Base):
    """An active signed browser session, indexed by a one-way keyed identifier."""

    __tablename__ = "auth_sessions"

    session_id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(80), nullable=False, default="personal")
    expires_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AuthLoginRateLimit(Base):
    """Persistent fixed-window login attempt bucket keyed by a keyed client hash."""

    __tablename__ = "auth_login_rate_limits"

    client_key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Workspace(Base):
    """A private personal workspace or an isolated guest demo workspace."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    seed_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delete_after_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class GuestUsage(Base):
    """Persistent request and provider-call counters for one guest scope."""

    __tablename__ = "guest_usage"

    scope_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    request_window_started_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_window_started_at_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
