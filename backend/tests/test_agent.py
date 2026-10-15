"""Agent runtime, permission, skill, and trace tests using temporary SQLite files."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import AgentTask, AgentTraceEvent
from app.services.agent_runtime import AgentRuntimeError, decide_task_approval
from app.services.tool_registry import TOOL_REGISTRY


@pytest.fixture
def agent_client(tmp_path: Path) -> Iterator[tuple[TestClient, sessionmaker[Session], str]]:
    database_path = tmp_path / "agent-test.sqlite3"
    database_url = f"sqlite:///{database_path.as_posix()}"
    test_engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

    def override_get_db() -> Iterator[Session]:
        with TestSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app) as client:
            yield client, TestSession, database_url
    finally:
        app.dependency_overrides.pop(get_db, None)
        test_engine.dispose()


def test_task_lifecycle_records_real_tool_trace_and_persists_after_reopen(agent_client) -> None:
    client, _, database_url = agent_client
    created = client.post(
        "/api/agent/tasks",
        json={"task_input": "Read the current UTC time", "tool_name": "current_datetime"},
    )

    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "completed"
    assert task["plan_steps"][0]["tool_name"] == "current_datetime"
    assert task["plan_steps"][0]["status"] == "completed"
    assert "registered tool step" in task["result_summary"]
    assert client.get(f"/api/agent/tasks/{task['id']}").json() == task

    trace_response = client.get(f"/api/agent/tasks/{task['id']}/trace")
    assert trace_response.status_code == 200
    trace = trace_response.json()
    event_types = [event["event_type"] for event in trace["events"]]
    assert event_types == [
        "task_created",
        "plan_created",
        "task_started",
        "tool_started",
        "tool_completed",
        "task_completed",
    ]
    plan_event = trace["events"][1]
    assert "no model call was made" in plan_event["message"]
    completed_tool = trace["events"][4]
    assert completed_tool["result_data"]["utc"].endswith("Z")

    reopened_engine = create_engine(database_url, connect_args={"check_same_thread": False})
    try:
        with Session(reopened_engine) as new_session:
            persisted = new_session.get(AgentTask, task["id"])
            persisted_events = new_session.scalars(
                select(AgentTraceEvent).where(AgentTraceEvent.task_id == task["id"])
            ).all()
            assert persisted is not None and persisted.status == "completed"
            assert len(persisted_events) == 6
    finally:
        reopened_engine.dispose()


def test_task_history_lists_persisted_tasks_newest_first_with_pagination(agent_client) -> None:
    client, _, _ = agent_client
    first = client.post(
        "/api/agent/tasks",
        json={"task_input": "Read UTC time", "tool_name": "current_datetime"},
    ).json()
    second = client.post(
        "/api/agent/tasks",
        json={"task_input": "Read UTC time again", "tool_name": "current_datetime"},
    ).json()

    page = client.get("/api/agent/tasks?limit=1&offset=0")
    next_page = client.get("/api/agent/tasks?limit=1&offset=1")

    assert page.status_code == 200
    assert [task["id"] for task in page.json()] == [second["id"]]
    assert [task["id"] for task in next_page.json()] == [first["id"]]
    assert client.get("/api/agent/tasks?limit=0").status_code == 422


def test_task_history_search_and_status_filters_match_persisted_tasks(agent_client) -> None:
    client, _, _ = agent_client
    match = client.post(
        "/api/agent/tasks",
        json={"task_input": "Prepare coastal trip notes", "tool_name": "current_datetime"},
    ).json()
    client.post(
        "/api/agent/tasks",
        json={"task_input": "Review portfolio", "tool_name": "current_datetime"},
    )
    waiting = client.post(
        "/api/agent/tasks",
        json={
            "task_input": "Save a coastal preference",
            "tool_name": "save_memory",
            "tool_input": {"content": "Likes quiet beaches"},
        },
    ).json()

    by_text = client.get("/api/agent/tasks?q=coastal")
    by_status = client.get("/api/agent/tasks?status=awaiting_approval")

    assert by_text.status_code == 200
    assert {task["id"] for task in by_text.json()} == {match["id"], waiting["id"]}
    assert [task["id"] for task in by_status.json()] == [waiting["id"]]
    assert client.get("/api/agent/tasks?q=not-found").json() == []
    assert client.get("/api/agent/tasks?status=cancelled").status_code == 422


def test_read_only_rerun_creates_a_new_linked_trace_without_mutating_original(agent_client) -> None:
    client, _, _ = agent_client
    original = client.post(
        "/api/agent/tasks",
        json={"task_input": "Read the current UTC time", "tool_name": "current_datetime"},
    ).json()
    original_trace_before = client.get(f"/api/agent/tasks/{original['id']}/trace").json()

    rerun_response = client.post(f"/api/agent/tasks/{original['id']}/rerun", json={})

    assert rerun_response.status_code == 201
    rerun = rerun_response.json()
    assert rerun["id"] != original["id"]
    assert rerun["task_input"] == original["task_input"]
    assert rerun["status"] == "completed"
    rerun_trace = client.get(f"/api/agent/tasks/{rerun['id']}/trace").json()
    created_event = rerun_trace["events"][0]
    assert created_event["result_data"] == {"rerun_of_task_id": original["id"]}
    assert "prior execution state was not resumed" in created_event["message"]
    original_trace_after = client.get(f"/api/agent/tasks/{original['id']}/trace").json()
    assert original_trace_after == original_trace_before


def test_write_rerun_requires_confirmation_and_still_waits_for_approval(agent_client) -> None:
    client, _, _ = agent_client
    original = client.post(
        "/api/agent/tasks",
        json={
            "task_input": "Save my preference",
            "tool_name": "save_memory",
            "tool_input": {"content": "Prefers quiet mornings"},
        },
    ).json()
    assert original["status"] == "awaiting_approval"
    assert client.post(
        f"/api/agent/tasks/{original['id']}/rerun",
        json={"confirm_write": True},
    ).status_code == 409
    assert client.post(
        f"/api/agent/tasks/{original['id']}/approval",
        json={"approved": True},
    ).json()["status"] == "completed"

    unconfirmed = client.post(f"/api/agent/tasks/{original['id']}/rerun", json={})
    assert unconfirmed.status_code == 409
    assert "Explicitly confirm" in unconfirmed.json()["detail"]
    assert len(client.get("/api/agent/tasks").json()) == 1
    assert len(client.get("/api/memories").json()) == 1

    confirmed = client.post(
        f"/api/agent/tasks/{original['id']}/rerun",
        json={"confirm_write": True},
    )
    assert confirmed.status_code == 201
    rerun = confirmed.json()
    assert rerun["id"] != original["id"]
    assert rerun["status"] == "awaiting_approval"
    assert len(client.get("/api/memories").json()) == 1

    completed = client.post(
        f"/api/agent/tasks/{rerun['id']}/approval",
        json={"approved": True},
    )
    assert completed.json()["status"] == "completed"
    assert len(client.get("/api/memories").json()) == 2


def test_write_rerun_confirmation_uses_registered_permissions_not_trace_metadata(agent_client) -> None:
    client, TestSession, _ = agent_client
    original = client.post(
        "/api/agent/tasks",
        json={
            "task_input": "Save my preference",
            "tool_name": "save_memory",
            "tool_input": {"content": "Prefers quiet mornings"},
        },
    ).json()
    client.post(f"/api/agent/tasks/{original['id']}/approval", json={"approved": True})

    with TestSession() as db:
        task = db.get(AgentTask, original["id"])
        assert task is not None
        plan = [dict(step) for step in task.plan_steps]
        plan[0]["permission_level"] = "read"
        plan[0]["requires_approval"] = False
        task.plan_steps = plan
        db.commit()

    response = client.post(f"/api/agent/tasks/{original['id']}/rerun", json={})

    assert response.status_code == 409
    assert "Explicitly confirm" in response.json()["detail"]
    assert len(client.get("/api/agent/tasks").json()) == 1
    assert len(client.get("/api/memories").json()) == 1


def test_stale_duplicate_approval_cannot_execute_a_write_twice(agent_client) -> None:
    client, TestSession, _ = agent_client
    waiting = client.post(
        "/api/agent/tasks",
        json={
            "task_input": "Save my preference",
            "tool_name": "save_memory",
            "tool_input": {"content": "Prefers quiet mornings"},
        },
    ).json()
    assert waiting["status"] == "awaiting_approval"

    SessionFactory = sessionmaker(
        bind=TestSession.kw["bind"],
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    first, stale = SessionFactory(), SessionFactory()
    try:
        first_task = first.get(AgentTask, waiting["id"])
        stale_task = stale.get(AgentTask, waiting["id"])
        assert first_task is not None and stale_task is not None
        first.commit()
        stale.commit()

        completed = decide_task_approval(first, waiting["id"], approved=True)
        assert completed.status == "completed"
        with pytest.raises(AgentRuntimeError) as rejected:
            decide_task_approval(stale, waiting["id"], approved=True)
        assert rejected.value.status_code == 409
        stale.rollback()
    finally:
        first.close()
        stale.close()

    assert len(client.get("/api/memories").json()) == 1
    trace = client.get(f"/api/agent/tasks/{waiting['id']}/trace").json()
    assert sum(event["event_type"] == "tool_started" for event in trace["events"]) == 1
    assert sum(event["event_type"] == "tool_completed" for event in trace["events"]) == 1


def test_overview_uses_persisted_memory_tasks_approvals_and_trace_events(agent_client) -> None:
    client, _, _ = agent_client
    memory = client.post(
        "/api/memories",
        json={"content": "Prefers concise project notes", "category": "preference"},
    ).json()
    completed = client.post(
        "/api/agent/tasks",
        json={"task_input": "Read UTC time", "tool_name": "current_datetime"},
    ).json()
    waiting = client.post(
        "/api/agent/tasks",
        json={
            "task_input": "Save a note after approval",
            "tool_name": "save_memory",
            "tool_input": {"content": "A user-approved note", "category": "general"},
        },
    ).json()

    response = client.get("/api/overview")
    assert response.status_code == 200
    overview = response.json()
    assert overview["memory_count"] == 1
    assert overview["task_count"] == 2
    assert overview["pending_approval_count"] == 1
    assert {task["id"] for task in overview["recent_tasks"]} == {
        completed["id"], waiting["id"]
    }
    assert any(event["task_id"] == completed["id"] for event in overview["recent_activity"])
    assert all("result_data" not in event and "input_data" not in event for event in overview["recent_activity"])
    assert client.get(f"/api/memories/{memory['id']}").status_code == 200


def test_end_to_end_memory_recall_demo_records_the_actual_local_result(agent_client) -> None:
    client, _, _ = agent_client
    memory_response = client.post(
        "/api/memories",
        json={"content": "The user enjoys coastal walks", "category": "preference"},
    )
    assert memory_response.status_code == 201
    memory = memory_response.json()

    task_response = client.post(
        "/api/agent/tasks",
        json={
            "task_input": "Find coastal walk notes",
            "tool_name": "memory_search",
            "tool_input": {"query": "coastal walks"},
        },
    )
    assert task_response.status_code == 201
    task = task_response.json()
    trace = client.get(f"/api/agent/tasks/{task['id']}/trace").json()
    completed = next(event for event in trace["events"] if event["event_type"] == "tool_completed")
    plan_event = next(event for event in trace["events"] if event["event_type"] == "plan_created")

    assert task["status"] == "completed"
    assert trace["task"]["id"] == task["id"]
    assert completed["task_id"] == task["id"]
    assert completed["input_data"] == {"query": "coastal walks", "limit": 10}
    assert completed["result_data"]["matches"][0]["id"] == memory["id"]
    assert completed["result_data"]["matches"][0]["content"] == "The user enjoys coastal walks"
    assert "Local deterministic plan" in plan_event["message"]
    assert "no model call was made" in plan_event["message"]
    assert all(event["tool_name"] == "memory_search" or event["tool_name"] is None for event in trace["events"])


def test_write_tool_waits_for_approval_before_mutating_memory(agent_client) -> None:
    client, _, _ = agent_client
    created = client.post(
        "/api/agent/tasks",
        json={
            "task_input": "Save my project preference",
            "tool_name": "save_memory",
            "tool_input": {
                "content": "Prefers short status summaries",
                "category": "preference",
                "source": "agent task",
            },
        },
    )

    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "awaiting_approval"
    assert client.get("/api/memories").json() == []
    waiting_trace = client.get(f"/api/agent/tasks/{task['id']}/trace").json()
    assert any(event["event_type"] == "approval_requested" for event in waiting_trace["events"])
    assert not any(event["event_type"] == "tool_started" for event in waiting_trace["events"])

    approved = client.post(
        f"/api/agent/tasks/{task['id']}/approval",
        json={"approved": True},
    )

    assert approved.status_code == 200
    assert approved.json()["status"] == "completed"
    saved = client.get("/api/memories").json()
    assert len(saved) == 1
    assert saved[0]["content"] == "Prefers short status summaries"
    final_trace = client.get(f"/api/agent/tasks/{task['id']}/trace").json()
    assert any(
        event["event_type"] == "approval_decision" and event["status"] == "approved"
        for event in final_trace["events"]
    )


def test_denied_tool_approval_is_traced_and_does_not_execute(agent_client) -> None:
    client, _, _ = agent_client
    task = client.post(
        "/api/agent/tasks",
        json={
            "task_input": "Save a note only if I approve",
            "tool_name": "save_memory",
            "tool_input": {"content": "Do not save without approval"},
        },
    ).json()

    denied = client.post(
        f"/api/agent/tasks/{task['id']}/approval",
        json={"approved": False},
    )
    trace = client.get(f"/api/agent/tasks/{task['id']}/trace").json()

    assert denied.status_code == 200
    assert denied.json()["status"] == "failed"
    assert client.get("/api/memories").json() == []
    assert any(
        event["event_type"] == "approval_decision" and event["status"] == "denied"
        for event in trace["events"]
    )
    assert not any(event["event_type"] == "tool_started" for event in trace["events"])


def test_each_write_step_in_a_skill_requires_its_own_task_approval(agent_client) -> None:
    client, _, _ = agent_client
    skill = client.post(
        "/api/agent/skills",
        json={
            "name": "Save two notes",
            "description": "Save each note only after its own user approval.",
            "steps": [
                {
                    "tool_name": "save_memory",
                    "description": "Save the first note",
                    "tool_input": {"content": "First user-approved note"},
                },
                {
                    "tool_name": "save_memory",
                    "description": "Save the second note",
                    "tool_input": {"content": "Second user-approved note"},
                },
            ],
        },
    ).json()
    client.post(f"/api/agent/skills/{skill['id']}/submit")
    client.post(f"/api/agent/skills/{skill['id']}/approve")

    task = client.post(
        "/api/agent/tasks",
        json={"task_input": "Run both note steps", "skill_id": skill["id"]},
    ).json()
    assert task["status"] == "awaiting_approval"
    assert client.get("/api/memories").json() == []

    resumed = client.post(f"/api/agent/tasks/{task['id']}/approval", json={"approved": True})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "awaiting_approval"
    memories = client.get("/api/memories").json()
    assert [memory["content"] for memory in memories] == ["First user-approved note"]

    completed = client.post(f"/api/agent/tasks/{task['id']}/approval", json={"approved": True})
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    memories = client.get("/api/memories").json()
    assert {memory["content"] for memory in memories} == {
        "First user-approved note",
        "Second user-approved note",
    }


def test_skills_require_submission_and_approval_and_disabled_skills_cannot_run(agent_client) -> None:
    client, _, _ = agent_client
    payload = {
        "name": "Read the clock",
        "description": "Read current UTC time with the local date tool.",
        "steps": [
            {
                "tool_name": "current_datetime",
                "description": "Read the current UTC time",
                "tool_input": {},
            }
        ],
    }
    created_response = client.post("/api/agent/skills", json=payload)
    assert created_response.status_code == 201
    skill = created_response.json()
    assert skill["status"] == "draft"
    assert client.get(f"/api/agent/skills/{skill['id']}").json() == skill
    assert client.post(
        "/api/agent/tasks",
        json={"task_input": "Run clock skill", "skill_id": skill["id"]},
    ).status_code == 409

    pending = client.post(f"/api/agent/skills/{skill['id']}/submit").json()
    assert pending["status"] == "pending_approval"
    approved = client.post(f"/api/agent/skills/{skill['id']}/approve").json()
    assert approved["status"] == "approved"

    task = client.post(
        "/api/agent/tasks",
        json={"task_input": "Run the approved clock skill", "skill_id": skill["id"]},
    )
    assert task.status_code == 201
    assert task.json()["status"] == "completed"
    assert task.json()["skill_id"] == skill["id"]

    skill_rerun = client.post(f"/api/agent/tasks/{task.json()['id']}/rerun", json={})
    assert skill_rerun.status_code == 201
    assert skill_rerun.json()["skill_id"] == skill["id"]

    disabled = client.post(f"/api/agent/skills/{skill['id']}/disable").json()
    assert disabled["status"] == "disabled"
    assert client.post(
        "/api/agent/tasks",
        json={"task_input": "Try disabled clock skill", "skill_id": skill["id"]},
    ).status_code == 409
    assert client.post(f"/api/agent/tasks/{task.json()['id']}/rerun", json={}).status_code == 409


def test_unknown_tools_invalid_tool_inputs_and_skill_definitions_are_rejected(agent_client) -> None:
    client, _, _ = agent_client
    unknown_task = client.post(
        "/api/agent/tasks",
        json={"task_input": "Try a shell", "tool_name": "shell", "tool_input": {"command": "dir"}},
    )
    invalid_tool_input = client.post(
        "/api/agent/tasks",
        json={"task_input": "Search without a query", "tool_name": "memory_search", "tool_input": {}},
    )
    unknown_skill = client.post(
        "/api/agent/skills",
        json={
            "name": "Unsafe skill",
            "description": "This tool does not exist.",
            "steps": [
                {
                    "tool_name": "python_exec",
                    "description": "Run arbitrary code",
                    "tool_input": {"code": "print('no')"},
                }
            ],
        },
    )

    assert unknown_task.status_code == 422
    assert "not in the registered allowlist" in unknown_task.json()["detail"]
    assert invalid_tool_input.status_code == 422
    assert "registered schema" in invalid_tool_input.json()["detail"]
    assert unknown_skill.status_code == 422
    assert "not in the registered allowlist" in unknown_skill.json()["detail"]
    assert client.get("/api/agent/tools").json()


def test_tool_failure_is_safe_and_persisted_in_trace(agent_client, monkeypatch) -> None:
    client, _, _ = agent_client
    definition = TOOL_REGISTRY["current_datetime"]

    def fail_with_sensitive_detail(_, __):
        raise RuntimeError("api_key=local-only-test-secret")

    monkeypatch.setitem(TOOL_REGISTRY, "current_datetime", replace(definition, execute=fail_with_sensitive_detail))
    created = client.post(
        "/api/agent/tasks",
        json={"task_input": "Read time", "tool_name": "current_datetime"},
    )

    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "failed"
    assert "local-only-test-secret" not in created.text
    trace = client.get(f"/api/agent/tasks/{task['id']}/trace")
    assert trace.status_code == 200
    assert "local-only-test-secret" not in trace.text
    events = trace.json()["events"]
    failure = next(event for event in events if event["event_type"] == "tool_failed")
    assert failure["status"] == "failed"
    assert "could not complete" in failure["message"]


def test_nonexistent_tasks_and_secret_like_task_inputs_are_handled_safely(agent_client) -> None:
    client, _, _ = agent_client
    missing = client.get("/api/agent/tasks/999999")
    missing_trace = client.get("/api/agent/tasks/999999/trace")
    secret_input = client.post(
        "/api/agent/tasks",
        json={"task_input": "NEBIUS_API_KEY=local-only-test-secret", "tool_name": "current_datetime"},
    )

    assert missing.status_code == 404
    assert missing_trace.status_code == 404
    assert secret_input.status_code == 422
    assert "local-only-test-secret" not in secret_input.text
