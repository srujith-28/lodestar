"""Deterministic, local task orchestration over the registered tool allowlist."""

from dataclasses import dataclass
from typing import Any

from fastapi import status
from pydantic import BaseModel
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api.agent_schemas import AgentTaskCreate
from app.api.memory_schemas import contains_credential
from app.models import AgentTask, AgentTraceEvent, Skill, utc_now
from app.services.tool_registry import (
    InvalidToolInputError,
    ToolDefinition,
    UnknownToolError,
    validate_tool_input,
)


class AgentRuntimeError(Exception):
    """An expected runtime condition with a safe HTTP status and message."""

    def __init__(self, status_code: int, public_message: str) -> None:
        super().__init__(public_message)
        self.status_code = status_code
        self.public_message = public_message


@dataclass(frozen=True)
class RuntimeStep:
    tool: ToolDefinition
    validated_input: BaseModel
    description: str


def _safe_json(value: Any, key: str | None = None) -> Any:
    """Redact recognized credentials before writing tool data into the trace."""
    if key and any(secret_word in key.lower() for secret_word in ("api_key", "token", "password", "secret", "credential")):
        return "[REDACTED]"
    if isinstance(value, str):
        return "[REDACTED]" if contains_credential(value) else value
    if isinstance(value, dict):
        return {str(field): _safe_json(item, str(field)) for field, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    return value


def _record_event(
    db: Session,
    task: AgentTask,
    *,
    event_type: str,
    status_value: str,
    step_index: int | None = None,
    tool_name: str | None = None,
    approval_required: bool = False,
    input_data: dict[str, Any] | None = None,
    result_data: Any | None = None,
    message: str | None = None,
) -> None:
    db.add(
        AgentTraceEvent(
            task_id=task.id,
            step_index=step_index,
            event_type=event_type,
            tool_name=tool_name,
            status=status_value,
            approval_required=approval_required,
            input_data=_safe_json(input_data) if input_data is not None else None,
            result_data=_safe_json(result_data) if result_data is not None else None,
            message=message,
        )
    )


def _prepare_step(tool_name: str, tool_input: dict[str, Any], description: str | None = None) -> RuntimeStep:
    try:
        tool, validated = validate_tool_input(tool_name, tool_input)
    except UnknownToolError:
        raise AgentRuntimeError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The requested tool is not in the registered allowlist.",
        ) from None
    except InvalidToolInputError:
        raise AgentRuntimeError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Tool input does not match its registered schema.",
        ) from None
    return RuntimeStep(
        tool=tool,
        validated_input=validated,
        description=description or tool.description,
    )


def _prepare_task_steps(db: Session, request: AgentTaskCreate) -> tuple[int | None, list[RuntimeStep]]:
    if request.tool_name is not None:
        return None, [_prepare_step(request.tool_name, request.tool_input)]

    if request.skill_id is None:
        raise AgentRuntimeError(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Choose one registered tool or approved skill.",
        )
    skill = db.get(Skill, request.skill_id)
    if skill is None:
        raise AgentRuntimeError(status.HTTP_404_NOT_FOUND, "Skill not found.")
    if skill.status != "approved":
        raise AgentRuntimeError(
            status.HTTP_409_CONFLICT,
            "Only approved skills can be run.",
        )

    steps = [
        _prepare_step(
            step["tool_name"],
            step.get("tool_input", {}),
            step.get("description"),
        )
        for step in skill.steps
    ]
    return skill.id, steps


def _step_plan(index: int, step: RuntimeStep) -> dict[str, Any]:
    return {
        "step": index + 1,
        "description": step.description,
        "tool_name": step.tool.name,
        "permission_level": step.tool.permission_level,
        "requires_approval": step.tool.requires_approval,
        "status": "pending",
        "input": _safe_json(step.validated_input.model_dump(mode="json")),
    }


def create_task(
    db: Session,
    request: AgentTaskCreate,
    *,
    rerun_from_task_id: int | None = None,
) -> AgentTask:
    skill_id, steps = _prepare_task_steps(db, request)
    plan = [_step_plan(index, step) for index, step in enumerate(steps)]
    task = AgentTask(
        skill_id=skill_id,
        task_input=request.task_input,
        status="pending",
        plan_steps=plan,
    )
    db.add(task)
    db.flush()
    _record_event(
        db,
        task,
        event_type="task_created",
        status_value="pending",
        result_data=(
            {"rerun_of_task_id": rerun_from_task_id}
            if rerun_from_task_id is not None
            else None
        ),
        message=(
            f"Created as a new run of task #{rerun_from_task_id}; "
            "the prior execution state was not resumed."
            if rerun_from_task_id is not None
            else "Task accepted for local deterministic execution."
        ),
    )
    _record_event(
        db,
        task,
        event_type="plan_created",
        status_value="pending",
        result_data=plan,
        message="Local deterministic plan created; no model call was made.",
    )
    db.commit()
    db.refresh(task)
    return _run_steps(db, task.id, steps, start_index=0, approval_granted=False)


def _run_steps(
    db: Session,
    task_id: int,
    steps: list[RuntimeStep],
    *,
    start_index: int,
    approval_granted: bool,
) -> AgentTask:
    task = db.get(AgentTask, task_id)
    if task is None:
        raise AgentRuntimeError(status.HTTP_404_NOT_FOUND, "Task not found.")
    if task.skill_id is not None:
        skill = db.get(Skill, task.skill_id)
        if skill is None or skill.status != "approved":
            task.status = "failed"
            task.result_summary = "The selected skill is no longer approved."
            _record_event(
                db,
                task,
                event_type="skill_not_approved",
                status_value="failed",
                message="The selected skill cannot be executed in its current state.",
            )
            db.commit()
            db.refresh(task)
            return task

    task.status = "running"
    task.updated_at = utc_now()
    _record_event(
        db,
        task,
        event_type="task_resumed" if start_index else "task_started",
        status_value="running",
        message="Registered local tool execution started.",
    )
    db.commit()

    plan = [dict(item) for item in task.plan_steps]
    completed_count = sum(1 for item in plan if item.get("status") == "completed")
    for index in range(start_index, len(steps)):
        step = steps[index]
        plan[index]["status"] = "running"
        task.plan_steps = plan
        task.updated_at = utc_now()

        if step.tool.requires_approval and not (approval_granted and index == start_index):
            plan[index]["status"] = "awaiting_approval"
            task.plan_steps = plan
            task.status = "awaiting_approval"
            task.updated_at = utc_now()
            input_data = step.validated_input.model_dump(mode="json")
            _record_event(
                db,
                task,
                event_type="approval_requested",
                status_value="awaiting_approval",
                step_index=index,
                tool_name=step.tool.name,
                approval_required=True,
                input_data=input_data,
                message="User approval is required before this tool can run.",
            )
            db.commit()
            db.refresh(task)
            return task

        _record_event(
            db,
            task,
            event_type="tool_started",
            status_value="running",
            step_index=index,
            tool_name=step.tool.name,
            approval_required=step.tool.requires_approval,
            input_data=step.validated_input.model_dump(mode="json"),
            message="Registered tool call started.",
        )
        db.commit()

        try:
            result = step.tool.execute(db, step.validated_input)
        except Exception:
            db.rollback()
            task = db.get(AgentTask, task_id)
            if task is None:
                raise AgentRuntimeError(status.HTTP_404_NOT_FOUND, "Task not found.") from None
            plan = [dict(item) for item in task.plan_steps]
            plan[index]["status"] = "failed"
            task.plan_steps = plan
            task.status = "failed"
            task.result_summary = "A registered tool failed. See the execution trace for its safe error status."
            task.updated_at = utc_now()
            _record_event(
                db,
                task,
                event_type="tool_failed",
                status_value="failed",
                step_index=index,
                tool_name=step.tool.name,
                approval_required=step.tool.requires_approval,
                input_data=step.validated_input.model_dump(mode="json"),
                message="The registered local tool could not complete.",
            )
            _record_event(
                db,
                task,
                event_type="task_failed",
                status_value="failed",
                message="Task execution stopped after a tool failure.",
            )
            db.commit()
            db.refresh(task)
            return task

        plan[index]["status"] = "completed"
        task.plan_steps = plan
        task.updated_at = utc_now()
        completed_count += 1
        _record_event(
            db,
            task,
            event_type="tool_completed",
            status_value="completed",
            step_index=index,
            tool_name=step.tool.name,
            approval_required=step.tool.requires_approval,
            input_data=step.validated_input.model_dump(mode="json"),
            result_data=result,
            message="Registered local tool completed.",
        )
        db.commit()
        task = db.get(AgentTask, task_id)
        if task is None:
            raise AgentRuntimeError(status.HTTP_404_NOT_FOUND, "Task not found.")
        plan = [dict(item) for item in task.plan_steps]

    task.status = "completed"
    task.result_summary = f"Completed {completed_count} registered tool step(s)."
    task.updated_at = utc_now()
    _record_event(
        db,
        task,
        event_type="task_completed",
        status_value="completed",
        message="All planned registered tool steps completed.",
    )
    db.commit()
    db.refresh(task)
    return task


def decide_task_approval(db: Session, task_id: int, approved: bool) -> AgentTask:
    task = db.get(AgentTask, task_id)
    if task is None:
        raise AgentRuntimeError(status.HTTP_404_NOT_FOUND, "Task not found.")
    if task.status != "awaiting_approval":
        raise AgentRuntimeError(
            status.HTTP_409_CONFLICT,
            "This task is not waiting for approval.",
        )

    plan = [dict(item) for item in task.plan_steps]
    waiting_index = next(
        (index for index, item in enumerate(plan) if item.get("status") == "awaiting_approval"),
        None,
    )
    if waiting_index is None:
        raise AgentRuntimeError(
            status.HTTP_409_CONFLICT,
            "This task has no tool awaiting approval.",
        )

    if approved and task.skill_id is not None:
        skill = db.get(Skill, task.skill_id)
        if skill is None or skill.status != "approved":
            task.status = "failed"
            task.result_summary = "The selected skill is no longer approved."
            task.updated_at = utc_now()
            _record_event(
                db,
                task,
                event_type="skill_not_approved",
                status_value="failed",
                message="The selected skill cannot be executed in its current state.",
            )
            db.commit()
            db.refresh(task)
            return task

    # Claim the pending decision in the database before recording it or running
    # a tool. Two requests can both read the old state; this conditional update
    # ensures only one can consume the approval and execute the write.
    claimed_status = "running" if approved else "failed"
    claim = db.execute(
        update(AgentTask)
        .where(AgentTask.id == task_id, AgentTask.status == "awaiting_approval")
        .values(status=claimed_status, updated_at=utc_now())
    )
    if claim.rowcount != 1:
        db.rollback()
        raise AgentRuntimeError(
            status.HTTP_409_CONFLICT,
            "This task is no longer waiting for approval.",
        )
    task.status = claimed_status
    task.updated_at = utc_now()

    if not approved:
        plan[waiting_index]["status"] = "denied"
        task.plan_steps = plan
        task.status = "failed"
        task.result_summary = "Required tool approval was denied."
        task.updated_at = utc_now()
        _record_event(
            db,
            task,
            event_type="approval_decision",
            status_value="denied",
            step_index=waiting_index,
            tool_name=plan[waiting_index]["tool_name"],
            approval_required=True,
            message="The user denied this tool execution.",
        )
        _record_event(
            db,
            task,
            event_type="task_failed",
            status_value="failed",
            message="Task stopped because required approval was denied.",
        )
        db.commit()
        db.refresh(task)
        return task

    _record_event(
        db,
        task,
        event_type="approval_decision",
        status_value="approved",
        step_index=waiting_index,
        tool_name=plan[waiting_index]["tool_name"],
        approval_required=True,
        message="The user approved this tool call.",
    )
    db.commit()

    steps = [
        _prepare_step(
            plan_step["tool_name"],
            plan_step.get("input", {}),
            plan_step.get("description"),
        )
        for plan_step in plan
    ]
    return _run_steps(db, task_id, steps, start_index=waiting_index, approval_granted=True)
