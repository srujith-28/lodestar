"""Agent task, trace, registered tool, and reusable skill endpoints."""

from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.agent_schemas import (
    AgentTaskApproval,
    AgentTaskCreate,
    AgentTaskPlanRequest,
    AgentTaskRerun,
    AgentTaskSearchRequest,
    AgentTaskResponse,
    AgentTaskTraceResponse,
    AgentTraceEventResponse,
    SkillCreate,
    SkillResponse,
    TaskStatus,
    ToolCatalogItem,
)
from app.core.config import Settings, get_settings
from app.database import get_db
from app.core.security import require_authenticated
from app.models import AgentTask, Skill, utc_now
from app.services.agent_runtime import (
    AgentRuntimeError,
    create_model_planned_task,
    create_task,
    decide_task_approval,
)
from app.services.tool_registry import (
    InvalidToolInputError,
    UnknownToolError,
    describe_tools,
    get_tool,
    validate_tool_input,
)


router = APIRouter(
    prefix="/api/agent",
    tags=["agent"],
    dependencies=[Depends(require_authenticated)],
)
skills_router = APIRouter(
    prefix="/api/agent/skills",
    tags=["skills"],
    dependencies=[Depends(require_authenticated)],
)
Result = TypeVar("Result")


def _database_operation(db: Session, operation: Callable[[], Result]) -> Result:
    try:
        return operation()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local agent storage is unavailable.",
        ) from None


def _runtime_operation(operation: Callable[[], Result], db: Session | None = None) -> Result:
    try:
        return operation()
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from None
    except SQLAlchemyError:
        if db is not None:
            db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local agent storage is unavailable.",
        ) from None


def _skill_or_404(db: Session, skill_id: int) -> Skill:
    skill = db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found.")
    return skill


def _validated_steps(request: SkillCreate) -> list[dict]:
    normalized: list[dict] = []
    for step in request.steps:
        try:
            _, validated_input = validate_tool_input(step.tool_name, step.tool_input)
        except UnknownToolError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Skill contains a tool that is not in the registered allowlist.",
            ) from None
        except InvalidToolInputError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Skill tool input does not match its registered schema.",
            ) from None
        normalized.append(
            {
                "tool_name": step.tool_name,
                "description": step.description,
                "tool_input": validated_input.model_dump(mode="json"),
            }
        )
    return normalized


@router.get("/tools", response_model=list[ToolCatalogItem])
def list_registered_tools() -> list[ToolCatalogItem]:
    return [ToolCatalogItem.model_validate(item) for item in describe_tools()]


@router.post("/tasks", response_model=AgentTaskResponse, status_code=status.HTTP_201_CREATED)
def submit_task(
    request: AgentTaskCreate,
    db: Session = Depends(get_db),
) -> AgentTaskResponse:
    task = _runtime_operation(lambda: create_task(db, request), db)
    return AgentTaskResponse.model_validate(task)


@router.post(
    "/tasks/plan",
    response_model=AgentTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_model_planned_task(
    request: AgentTaskPlanRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AgentTaskResponse:
    """Persist a model planning attempt, then execute only locally validated tools."""
    try:
        task = await create_model_planned_task(db, request, settings)
    except AgentRuntimeError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Local agent storage is unavailable.",
        ) from None
    return AgentTaskResponse.model_validate(task)


@router.get("/tasks", response_model=list[AgentTaskResponse])
def list_tasks(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
) -> list[AgentTaskResponse]:
    """List persisted tasks; private text searches use POST /tasks/search."""
    if "q" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Send history search text in the POST /api/agent/tasks/search body.",
        )

    statement = select(AgentTask)
    if task_status:
        statement = statement.where(AgentTask.status == task_status)
    statement = statement.order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
    statement = statement.offset(offset).limit(limit)
    tasks = _database_operation(
        db,
        lambda: db.scalars(statement).all(),
    )
    return [AgentTaskResponse.model_validate(task) for task in tasks]


@router.post("/tasks/search", response_model=list[AgentTaskResponse])
def search_tasks(
    request: AgentTaskSearchRequest,
    db: Session = Depends(get_db),
) -> list[AgentTaskResponse]:
    """Search and filter persisted history without placing task text in the URL."""
    statement = select(AgentTask)
    query = request.q or ""
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        statement = statement.where(
            or_(
                AgentTask.task_input.ilike(pattern, escape="\\"),
                AgentTask.result_summary.ilike(pattern, escape="\\"),
            )
        )
    if request.status:
        statement = statement.where(AgentTask.status == request.status)
    statement = statement.order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
    statement = statement.offset(request.offset).limit(request.limit)
    tasks = _database_operation(db, lambda: db.scalars(statement).all())
    return [AgentTaskResponse.model_validate(task) for task in tasks]


@router.get("/tasks/{task_id}", response_model=AgentTaskResponse)
def get_task(
    task_id: int = Path(gt=0),
    db: Session = Depends(get_db),
) -> AgentTaskResponse:
    task = _database_operation(db, lambda: db.get(AgentTask, task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return AgentTaskResponse.model_validate(task)


@router.get("/tasks/{task_id}/trace", response_model=AgentTaskTraceResponse)
def get_task_trace(
    task_id: int = Path(gt=0),
    db: Session = Depends(get_db),
) -> AgentTaskTraceResponse:
    task = _database_operation(db, lambda: db.get(AgentTask, task_id))
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    events = _database_operation(db, lambda: list(task.trace_events))
    return AgentTaskTraceResponse(
        task=AgentTaskResponse.model_validate(task),
        events=[AgentTraceEventResponse.model_validate(event) for event in events],
    )


@router.post(
    "/tasks/{task_id}/rerun",
    response_model=AgentTaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def rerun_task(
    request: AgentTaskRerun,
    task_id: int = Path(gt=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AgentTaskResponse:
    """Create a new run from a saved task; never resume old in-memory execution."""
    source = _database_operation(db, lambda: db.get(AgentTask, task_id))
    if source is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    if source.status not in {"completed", "failed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only completed or failed tasks can be run again.",
        )

    plan = source.plan_steps or []
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This task has no saved plan that can be run again.",
        )

    model_planned = any(event.event_type == "planner_started" for event in source.trace_events)
    if source.skill_id is not None:
        skill = _database_operation(db, lambda: db.get(Skill, source.skill_id))
        if skill is None or skill.status != "approved":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The source skill is no longer approved and cannot be run again.",
            )
        tool_names = [step.get("tool_name") for step in skill.steps]
    elif model_planned:
        tool_names = [step.get("tool_name") for step in plan]
    elif len(plan) == 1:
        tool_names = [plan[0].get("tool_name")]
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This task's saved plan cannot be safely reconstructed.",
        )

    try:
        tools = [get_tool(tool_name) for tool_name in tool_names]
    except (UnknownToolError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This task contains a tool that is no longer in the registered allowlist.",
        ) from None
    may_write = any(tool.permission_level == "write" or tool.requires_approval for tool in tools)
    if may_write and not request.confirm_write:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This task may repeat write actions. Explicitly confirm before creating a new run.",
        )

    if source.skill_id is not None:
        rerun_request = AgentTaskCreate(
            task_input=source.task_input,
            skill_id=source.skill_id,
        )
    elif model_planned:
        rerun_request = AgentTaskPlanRequest(task_input=source.task_input)
        try:
            task = await create_model_planned_task(
                db,
                rerun_request,
                settings,
                rerun_from_task_id=source.id,
            )
        except AgentRuntimeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.public_message) from None
        except SQLAlchemyError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Local agent storage is unavailable.",
            ) from None
        return AgentTaskResponse.model_validate(task)
    elif len(plan) == 1:
        rerun_request = AgentTaskCreate(
            task_input=source.task_input,
            tool_name=plan[0].get("tool_name"),
            tool_input=plan[0].get("input", {}),
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This task's saved plan cannot be safely reconstructed.",
        )

    task = _runtime_operation(
        lambda: create_task(db, rerun_request, rerun_from_task_id=source.id),
        db,
    )
    return AgentTaskResponse.model_validate(task)


@router.post("/tasks/{task_id}/approval", response_model=AgentTaskResponse)
def approve_or_deny_task(
    request: AgentTaskApproval,
    task_id: int = Path(gt=0),
    db: Session = Depends(get_db),
) -> AgentTaskResponse:
    task = _runtime_operation(lambda: decide_task_approval(db, task_id, request.approved), db)
    return AgentTaskResponse.model_validate(task)


@skills_router.get("", response_model=list[SkillResponse])
def list_skills(db: Session = Depends(get_db)) -> list[SkillResponse]:
    skills = _database_operation(
        db,
        lambda: db.scalars(select(Skill).order_by(Skill.updated_at.desc(), Skill.id.desc())).all(),
    )
    return [SkillResponse.model_validate(skill) for skill in skills]


@skills_router.post("", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
def create_skill(
    request: SkillCreate,
    db: Session = Depends(get_db),
) -> SkillResponse:
    steps = _validated_steps(request)

    def save() -> Skill:
        skill = Skill(
            name=request.name,
            description=request.description,
            steps=steps,
            status="draft",
        )
        db.add(skill)
        db.commit()
        db.refresh(skill)
        return skill

    skill = _database_operation(db, save)
    return SkillResponse.model_validate(skill)


@skills_router.get("/{skill_id}", response_model=SkillResponse)
def get_skill(
    skill_id: int = Path(gt=0),
    db: Session = Depends(get_db),
) -> SkillResponse:
    skill = _database_operation(db, lambda: _skill_or_404(db, skill_id))
    return SkillResponse.model_validate(skill)


@skills_router.post("/{skill_id}/submit", response_model=SkillResponse)
def submit_skill_for_approval(
    skill_id: int = Path(gt=0),
    db: Session = Depends(get_db),
) -> SkillResponse:
    def submit() -> Skill:
        skill = _skill_or_404(db, skill_id)
        if skill.status != "draft":
            raise HTTPException(status_code=409, detail="Only draft skills can be submitted.")
        skill.status = "pending_approval"
        skill.updated_at = utc_now()
        db.commit()
        db.refresh(skill)
        return skill

    skill = _database_operation(db, submit)
    return SkillResponse.model_validate(skill)


@skills_router.post("/{skill_id}/approve", response_model=SkillResponse)
def approve_skill(
    skill_id: int = Path(gt=0),
    db: Session = Depends(get_db),
) -> SkillResponse:
    def approve() -> Skill:
        skill = _skill_or_404(db, skill_id)
        if skill.status != "pending_approval":
            raise HTTPException(
                status_code=409,
                detail="Only skills pending approval can be approved.",
            )
        skill.status = "approved"
        skill.updated_at = utc_now()
        db.commit()
        db.refresh(skill)
        return skill

    skill = _database_operation(db, approve)
    return SkillResponse.model_validate(skill)


@skills_router.post("/{skill_id}/disable", response_model=SkillResponse)
def disable_skill(
    skill_id: int = Path(gt=0),
    db: Session = Depends(get_db),
) -> SkillResponse:
    def disable() -> Skill:
        skill = _skill_or_404(db, skill_id)
        skill.status = "disabled"
        skill.updated_at = utc_now()
        db.commit()
        db.refresh(skill)
        return skill

    skill = _database_operation(db, disable)
    return SkillResponse.model_validate(skill)
