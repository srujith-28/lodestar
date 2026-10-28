"""Idempotent cleanup for expired guest workspaces.

Run this module as a one-shot scheduled job. It does not start a process-local
timer, so cleanup continues to be schedulable when a Render web service sleeps.
"""

from __future__ import annotations

import sys
import time

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import SESSION_MAX_AGE_SECONDS
from app.database import SessionLocal
from app.models import (
    AgentTask,
    AgentTraceEvent,
    AuthSession,
    GuestUsage,
    Memory,
    Skill,
    Workspace,
)


RETENTION_SECONDS_PER_DAY = 86_400
DEFAULT_CLEANUP_BATCH_SIZE = 100


class GuestRetentionCleanupError(RuntimeError):
    """Safe, non-sensitive failure raised when retention storage is unavailable."""


def purge_expired_guest_workspaces(
    db: Session,
    settings: Settings,
    *,
    now: int | None = None,
    batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE,
) -> int:
    """Purge guest workspaces past their retention deadline, without touching personal data.

    A workspace row is claimed with one conditional DELETE ... RETURNING statement
    inside the same transaction as its dependent-record deletes. Concurrent job
    instances can therefore claim a workspace at most once; a rollback restores
    both the workspace and its associated records together.

    Older guest rows with no deadline receive a full retention window plus the
    maximum session lifetime from the first cleanup run. This preserves data
    safely while migrating existing guests into the retention policy.
    """
    current_time = int(time.time()) if now is None else now
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    retention_seconds = settings.guest_data_retention_days * RETENTION_SECONDS_PER_DAY
    legacy_deadline = current_time + SESSION_MAX_AGE_SECONDS + retention_seconds

    try:
        with db.begin():
            # Atomic and one-time: legacy guests get a complete grace period from
            # this run, even if their old session row was already revoked/deleted.
            db.execute(
                update(Workspace)
                .where(
                    Workspace.kind == "guest",
                    Workspace.delete_after_epoch.is_(None),
                )
                .values(delete_after_epoch=legacy_deadline)
            )

            active_session = (
                select(AuthSession.session_id_hash)
                .where(
                    AuthSession.workspace_id == Workspace.id,
                    AuthSession.expires_at_epoch > current_time,
                )
                .exists()
            )
            eligible_ids = (
                select(Workspace.id)
                .where(
                    Workspace.kind == "guest",
                    Workspace.delete_after_epoch <= current_time,
                    ~active_session,
                )
                .order_by(Workspace.delete_after_epoch, Workspace.id)
                .limit(batch_size)
            )
            claimed_ids = list(
                db.scalars(
                    delete(Workspace)
                    .where(
                        Workspace.id.in_(eligible_ids),
                        Workspace.kind == "guest",
                        Workspace.delete_after_epoch <= current_time,
                        ~active_session,
                    )
                    .returning(Workspace.id)
                ).all()
            )

            for workspace_id in claimed_ids:
                task_ids = select(AgentTask.id).where(
                    AgentTask.workspace_id == workspace_id
                )
                db.execute(
                    delete(AgentTraceEvent).where(AgentTraceEvent.task_id.in_(task_ids))
                )
                db.execute(delete(AgentTask).where(AgentTask.workspace_id == workspace_id))
                db.execute(delete(Skill).where(Skill.workspace_id == workspace_id))
                db.execute(delete(Memory).where(Memory.workspace_id == workspace_id))
                db.execute(
                    delete(AuthSession).where(AuthSession.workspace_id == workspace_id)
                )
                # Per-workspace counters use the workspace ID as their scope key.
                # The shared global provider-cap row intentionally remains.
                db.execute(
                    delete(GuestUsage).where(GuestUsage.scope_key == workspace_id)
                )

        return len(claimed_ids)
    except SQLAlchemyError:
        db.rollback()
        raise GuestRetentionCleanupError(
            "Guest retention cleanup could not access its database."
        ) from None


def main() -> int:
    """Run one scheduled cleanup and print only an aggregate workspace count."""
    settings = get_settings()
    try:
        with SessionLocal() as db:
            deleted_count = purge_expired_guest_workspaces(db, settings)
    except GuestRetentionCleanupError:
        print("Guest retention cleanup failed; database details were withheld.", file=sys.stderr)
        return 1
    print(f"Guest retention cleanup completed; expired workspaces removed: {deleted_count}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
