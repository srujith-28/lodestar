"""Idempotent, explicitly labeled seed data for isolated guest workspaces."""

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.agent_schemas import AgentTaskCreate
from app.models import AgentTask, AgentTraceEvent, Memory, Skill, Workspace
from app.services.agent_runtime import create_task


SAMPLE_MEMORIES = (
    {
        "content": (
            "Aurora Field Guide goal: help Bayview Coastal Reserve volunteers create a concise, "
            "accessible seasonal guide to native plants. This is fictional sample project context."
        ),
        "category": "project",
        "source": "sample workspace",
    },
    {
        "content": (
            "Aurora Field Guide decision: use common plant names first, add one practical "
            "identification note, and never collect plants. This is fictional sample project context."
        ),
        "category": "decision",
        "source": "sample workspace",
    },
    {
        "content": (
            "Fictional sample preference: the Aurora Field Guide project owner prefers concise "
            "summaries with clear headings and practical next steps."
        ),
        "category": "preference",
        "source": "sample workspace",
    },
)


def _remove_partial_sample(db: Session, workspace_id: str) -> None:
    sample_task_ids = select(AgentTask.id).where(
        AgentTask.workspace_id == workspace_id,
        AgentTask.is_sample.is_(True),
    )
    db.execute(delete(AgentTraceEvent).where(AgentTraceEvent.task_id.in_(sample_task_ids)))
    db.execute(delete(AgentTask).where(AgentTask.id.in_(sample_task_ids)))
    db.execute(
        delete(Memory).where(
            Memory.workspace_id == workspace_id,
            Memory.is_sample.is_(True),
        )
    )


def seed_guest_workspace(db: Session, workspace_id: str) -> None:
    """Seed one sample set and execute its read-only sample search exactly once."""
    workspace = db.get(Workspace, workspace_id)
    if workspace is None or workspace.kind != "guest" or workspace.seed_version >= 1:
        return

    _remove_partial_sample(db, workspace_id)
    for memory_data in SAMPLE_MEMORIES:
        db.add(Memory(workspace_id=workspace_id, is_sample=True, **memory_data))
    db.commit()

    # This is an actual local tool run over the just-seeded sample records. No
    # provider response is fabricated and no saved memory is sent to Nebius.
    task = create_task(
        db,
        AgentTaskCreate(
            task_input="Find the goal and key decision for the Aurora Field Guide.",
            tool_name="memory_search",
            tool_input={"query": "Aurora Field Guide", "limit": 10},
        ),
        workspace_id=workspace_id,
    )
    task.is_sample = True
    workspace = db.get(Workspace, workspace_id)
    if workspace is not None:
        workspace.seed_version = 1
    db.commit()


def reset_guest_workspace(db: Session, workspace_id: str) -> None:
    """Delete only the caller's guest records, then recreate its sample dataset."""
    workspace = db.get(Workspace, workspace_id)
    if workspace is None or workspace.kind != "guest":
        return

    task_ids = select(AgentTask.id).where(AgentTask.workspace_id == workspace_id)
    db.execute(delete(AgentTraceEvent).where(AgentTraceEvent.task_id.in_(task_ids)))
    db.execute(delete(AgentTask).where(AgentTask.workspace_id == workspace_id))
    db.execute(delete(Skill).where(Skill.workspace_id == workspace_id))
    db.execute(delete(Memory).where(Memory.workspace_id == workspace_id))
    workspace.seed_version = 0
    db.commit()
    seed_guest_workspace(db, workspace_id)
