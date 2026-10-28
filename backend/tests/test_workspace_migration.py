"""Additive workspace migration preserves rows from the existing schema."""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.database import Base, migrate_workspace_columns


def test_workspace_migration_assigns_existing_rows_to_personal_and_is_repeatable(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'pre-workspace.sqlite3').as_posix()}")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL, "
            "category VARCHAR(60) NOT NULL, source VARCHAR(80) NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO memories (id, content, category, source, created_at, updated_at) "
            "VALUES (1, 'existing private note', 'note', 'user', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "CREATE TABLE agent_tasks (id INTEGER PRIMARY KEY, skill_id INTEGER, task_input TEXT NOT NULL, "
            "status VARCHAR(32) NOT NULL, plan_steps JSON NOT NULL, result_summary TEXT, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO agent_tasks (id, task_input, status, plan_steps, created_at, updated_at) "
            "VALUES (7, 'existing task', 'completed', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "CREATE TABLE skills (id INTEGER PRIMARY KEY, name VARCHAR(80) NOT NULL, "
            "description VARCHAR(500) NOT NULL, steps JSON NOT NULL, status VARCHAR(32) NOT NULL, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO skills (id, name, description, steps, status, created_at, updated_at) "
            "VALUES (3, 'existing skill', 'preserved skill', '[]', 'disabled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "CREATE TABLE auth_sessions (session_id_hash VARCHAR(64) PRIMARY KEY, "
            "expires_at_epoch INTEGER NOT NULL, created_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO auth_sessions (session_id_hash, expires_at_epoch, created_at) "
            "VALUES (:session_hash, 9999999999, CURRENT_TIMESTAMP)"
        ), {"session_hash": "a" * 64})
        connection.execute(text(
            "CREATE TABLE workspaces (id VARCHAR(80) PRIMARY KEY, kind VARCHAR(16) NOT NULL, "
            "seed_version INTEGER NOT NULL, created_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO workspaces (id, kind, seed_version, created_at) "
            "VALUES ('guest_legacy', 'guest', 1, CURRENT_TIMESTAMP)"
        ))

    # Create only the newly introduced tables, leaving legacy table definitions intact.
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_workspace_columns(engine)
    migrate_workspace_columns(engine)

    inspector = inspect(engine)
    for table_name in ("memories", "agent_tasks", "skills", "auth_sessions"):
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        assert "workspace_id" in columns
    assert {"workspace_id", "is_sample"}.issubset(
        {column["name"] for column in inspector.get_columns("memories")}
    )
    assert "delete_after_epoch" in {
        column["name"] for column in inspector.get_columns("workspaces")
    }
    with engine.connect() as connection:
        memory = connection.execute(
            text("SELECT content, workspace_id, is_sample FROM memories WHERE id = 1")
        ).one()
        task = connection.execute(
            text("SELECT task_input, workspace_id, is_sample FROM agent_tasks WHERE id = 7")
        ).one()
        assert tuple(memory) == ("existing private note", "personal", 0)
        assert tuple(task) == ("existing task", "personal", 0)
        assert connection.execute(
            text("SELECT workspace_id FROM skills WHERE id = 3")
        ).scalar_one() == "personal"
        assert connection.execute(
            text("SELECT workspace_id FROM auth_sessions WHERE session_id_hash = :session_hash"),
            {"session_hash": "a" * 64},
        ).scalar_one() == "personal"
        assert connection.execute(
            text("SELECT kind FROM workspaces WHERE id = 'personal'")
        ).scalar_one() == "personal"
        assert connection.execute(
            text("SELECT delete_after_epoch FROM workspaces WHERE id = 'guest_legacy'")
        ).scalar_one() is None
    engine.dispose()


def test_workspace_migration_on_fresh_sqlite_is_repeatable(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'fresh-workspace.sqlite3').as_posix()}")

    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_workspace_columns(engine)
    migrate_workspace_columns(engine)

    with Session(engine) as session:
        personal_rows = session.scalars(
            text("SELECT id FROM workspaces WHERE id = 'personal'")
        ).all()
        assert personal_rows == ["personal"]
        assert "delete_after_epoch" in {
            column["name"] for column in inspect(engine).get_columns("workspaces")
        }
        for table_name in ("memories", "agent_tasks", "skills", "auth_sessions"):
            assert "workspace_id" in {
                column["name"] for column in inspect(engine).get_columns(table_name)
            }
    engine.dispose()
