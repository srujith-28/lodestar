"""Guest retention deletes only expired guest-owned data, transactionally."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import SESSION_MAX_AGE_SECONDS
from app.database import Base
from app.models import (
    AgentTask,
    AgentTraceEvent,
    AuthSession,
    GuestUsage,
    Memory,
    Skill,
    Workspace,
)
from app.services.guest_retention_cleanup import purge_expired_guest_workspaces


RETENTION_DAYS = 7
TEST_SECRET = SecretStr("retention-test-session-secret-at-least-32-characters")


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        guest_demo_enabled=True,
        lodestar_session_secret=TEST_SECRET,
        guest_data_retention_days=RETENTION_DAYS,
    )


def _engine(tmp_path, name: str = "retention.sqlite3"):
    engine = create_engine(
        f"sqlite:///{(tmp_path / name).as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(bind=engine)
    return engine


def test_cleanup_removes_expired_guest_records_but_preserves_personal_and_future_data(
    tmp_path,
) -> None:
    engine = _engine(tmp_path)
    now = 2_000_000_000
    expired_id = "guest_expired"
    future_id = "guest_future"
    personal_id = "personal"
    with Session(engine) as db:
        db.add_all(
            [
                Workspace(id=expired_id, kind="guest", delete_after_epoch=now - 1),
                Workspace(id=future_id, kind="guest", delete_after_epoch=now + 1),
                Workspace(id=personal_id, kind="personal", delete_after_epoch=now - 10_000),
            ]
        )
        db.flush()

        expired_task = AgentTask(
            workspace_id=expired_id,
            task_input="expired guest task",
            status="completed",
            plan_steps=[],
        )
        personal_task = AgentTask(
            workspace_id=personal_id,
            task_input="personal task",
            status="completed",
            plan_steps=[],
        )
        db.add_all([expired_task, personal_task])
        db.flush()
        expired_task_id = expired_task.id
        personal_task_id = personal_task.id
        db.add_all(
            [
                AgentTraceEvent(
                    task_id=expired_task.id,
                    event_type="task_completed",
                    status="completed",
                ),
                AgentTraceEvent(
                    task_id=personal_task.id,
                    event_type="task_completed",
                    status="completed",
                ),
                Memory(workspace_id=expired_id, content="expired note", category="note", source="test"),
                Memory(workspace_id=personal_id, content="personal note", category="note", source="test"),
                Skill(
                    workspace_id=expired_id,
                    name="Expired skill",
                    description="test",
                    steps=[],
                    status="draft",
                ),
                Skill(
                    workspace_id=personal_id,
                    name="Personal skill",
                    description="test",
                    steps=[],
                    status="draft",
                ),
                AuthSession(
                    session_id_hash="a" * 64,
                    workspace_id=expired_id,
                    expires_at_epoch=now - 1,
                ),
                AuthSession(
                    session_id_hash="b" * 64,
                    workspace_id=personal_id,
                    expires_at_epoch=now + 100,
                ),
                GuestUsage(
                    scope_key=expired_id,
                    request_window_started_at_epoch=now,
                    request_count=1,
                    provider_window_started_at_epoch=now,
                    provider_call_count=1,
                ),
                GuestUsage(
                    scope_key="global_provider_calls",
                    request_window_started_at_epoch=now,
                    request_count=0,
                    provider_window_started_at_epoch=now,
                    provider_call_count=1,
                ),
            ]
        )
        db.commit()

        assert purge_expired_guest_workspaces(db, _settings(), now=now) == 1
        assert db.get(Workspace, expired_id) is None
        assert db.get(Workspace, future_id) is not None
        assert db.get(Workspace, personal_id) is not None
        assert db.scalar(select(AgentTask).where(AgentTask.workspace_id == expired_id)) is None
        assert db.scalar(
            select(AgentTraceEvent).where(AgentTraceEvent.task_id == expired_task_id)
        ) is None
        assert db.scalar(select(Memory).where(Memory.workspace_id == expired_id)) is None
        assert db.scalar(select(Skill).where(Skill.workspace_id == expired_id)) is None
        assert db.scalar(
            select(AuthSession).where(AuthSession.workspace_id == expired_id)
        ) is None
        assert db.get(GuestUsage, expired_id) is None
        assert db.get(GuestUsage, "global_provider_calls") is not None
        assert db.scalar(select(Memory).where(Memory.workspace_id == personal_id)) is not None
        assert db.scalar(select(AgentTask).where(AgentTask.workspace_id == personal_id)) is not None
        assert db.scalar(select(Skill).where(Skill.workspace_id == personal_id)) is not None
        assert db.scalar(select(AuthSession).where(AuthSession.workspace_id == personal_id)) is not None

    engine.dispose()


def test_cleanup_is_repeatable_and_defers_legacy_guests_for_full_retention(tmp_path) -> None:
    engine = _engine(tmp_path)
    now = 2_000_000_000
    with Session(engine) as db:
        db.add(Workspace(id="guest_expired", kind="guest", delete_after_epoch=now - 1))
        db.add(Workspace(id="guest_legacy", kind="guest", delete_after_epoch=None))
        db.add(
            AuthSession(
                session_id_hash="c" * 64,
                workspace_id="guest_legacy",
                expires_at_epoch=now + SESSION_MAX_AGE_SECONDS,
            )
        )
        db.commit()

        assert purge_expired_guest_workspaces(db, _settings(), now=now) == 1
        db.commit()
        legacy = db.get(Workspace, "guest_legacy")
        assert legacy is not None
        assert legacy.delete_after_epoch == (
            now + SESSION_MAX_AGE_SECONDS + RETENTION_DAYS * 86_400
        )
        db.commit()
        assert purge_expired_guest_workspaces(db, _settings(), now=now) == 0
        assert db.scalar(
            select(func.count()).select_from(Workspace).where(Workspace.id == "guest_expired")
        ) == 0
    engine.dispose()


def test_cleanup_never_deletes_a_guest_with_an_active_session(tmp_path) -> None:
    engine = _engine(tmp_path)
    now = 2_000_000_000
    with Session(engine) as db:
        db.add(Workspace(id="guest_active", kind="guest", delete_after_epoch=now - 1))
        db.add(
            AuthSession(
                session_id_hash="d" * 64,
                workspace_id="guest_active",
                expires_at_epoch=now + 60,
            )
        )
        db.commit()
        assert purge_expired_guest_workspaces(db, _settings(), now=now) == 0
        assert db.get(Workspace, "guest_active") is not None
    engine.dispose()


def test_concurrent_cleanup_runs_claim_an_expired_workspace_once(tmp_path) -> None:
    engine = _engine(tmp_path, "concurrent-retention.sqlite3")
    now = 2_000_000_000
    with Session(engine) as db:
        db.add(Workspace(id="guest_concurrent", kind="guest", delete_after_epoch=now - 1))
        db.add(Memory(workspace_id="guest_concurrent", content="note", category="note", source="test"))
        db.commit()

    barrier = Barrier(2)

    def run_cleanup() -> int:
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return purge_expired_guest_workspaces(db, _settings(), now=now)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: run_cleanup(), range(2)))
        assert sum(results) == 1
        with Session(engine) as db:
            assert db.get(Workspace, "guest_concurrent") is None
            assert db.scalar(
                select(Memory).where(Memory.workspace_id == "guest_concurrent")
            ) is None
    finally:
        engine.dispose()
