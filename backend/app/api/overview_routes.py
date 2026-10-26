"""Read-only local dashboard data assembled from persisted application records."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.agent_schemas import AgentTaskResponse
from app.database import get_db
from app.core.security import require_authenticated
from app.models import AgentTask, AgentTraceEvent, Memory


router = APIRouter(
    prefix="/api",
    tags=["overview"],
    dependencies=[Depends(require_authenticated)],
)


class RecentActivityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    event_type: str
    tool_name: str | None
    status: str
    approval_required: bool
    created_at: datetime


class OverviewResponse(BaseModel):
    memory_count: int
    task_count: int
    pending_approval_count: int
    recent_tasks: list[AgentTaskResponse]
    recent_activity: list[RecentActivityResponse]


@router.get("/overview", response_model=OverviewResponse)
def get_overview(db: Session = Depends(get_db)) -> OverviewResponse:
    """Return counts and recent persisted local activity; never calls a provider."""
    try:
        memory_count = db.scalar(select(func.count()).select_from(Memory)) or 0
        task_count = db.scalar(select(func.count()).select_from(AgentTask)) or 0
        pending_approval_count = db.scalar(
            select(func.count()).select_from(AgentTask).where(
                AgentTask.status == "awaiting_approval"
            )
        ) or 0
        recent_tasks = db.scalars(
            select(AgentTask)
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
            .limit(5)
        ).all()
        recent_activity = db.scalars(
            select(AgentTraceEvent)
            .join(AgentTask, AgentTask.id == AgentTraceEvent.task_id)
            .order_by(AgentTraceEvent.created_at.desc(), AgentTraceEvent.id.desc())
            .limit(8)
        ).all()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local dashboard data is unavailable.",
        ) from None

    return OverviewResponse(
        memory_count=memory_count,
        task_count=task_count,
        pending_approval_count=pending_approval_count,
        recent_tasks=[AgentTaskResponse.model_validate(task) for task in recent_tasks],
        recent_activity=[
            RecentActivityResponse.model_validate(event) for event in recent_activity
        ],
    )
