"""SQLAlchemy database setup for local SQLite development."""

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    """Base class for database models added in later milestones."""


settings = get_settings()
database_url = settings.database_url.get_secret_value()
connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session and always close it after the request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def initialize_database() -> None:
    """Create tables and apply safe, additive workspace-column migrations."""
    # Importing the model registers it with Base.metadata before create_all runs.
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    migrate_workspace_columns(engine)


def migrate_workspace_columns(bind) -> None:
    """Assign pre-existing rows to `personal` without rewriting or dropping data."""
    additive_columns = {
        "memories": {
            "workspace_id": "VARCHAR(80) NOT NULL DEFAULT 'personal'",
            "is_sample": "BOOLEAN NOT NULL DEFAULT FALSE",
        },
        "agent_tasks": {
            "workspace_id": "VARCHAR(80) NOT NULL DEFAULT 'personal'",
            "is_sample": "BOOLEAN NOT NULL DEFAULT FALSE",
        },
        "skills": {"workspace_id": "VARCHAR(80) NOT NULL DEFAULT 'personal'"},
        "auth_sessions": {"workspace_id": "VARCHAR(80) NOT NULL DEFAULT 'personal'"},
    }
    with bind.begin() as connection:
        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())
        for table_name, columns in additive_columns.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
            for column_name, definition in columns.items():
                if column_name not in existing_columns:
                    connection.execute(
                        text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}')
                    )
                    existing_columns.add(column_name)
        for table_name in additive_columns:
            if table_name in existing_tables:
                connection.execute(
                    text(
                        f'CREATE INDEX IF NOT EXISTS "ix_{table_name}_workspace_id" '
                        f'ON "{table_name}" ("workspace_id")'
                    )
                )
        if "workspaces" in existing_tables:
            workspace_columns = {
                column["name"] for column in inspect(connection).get_columns("workspaces")
            }
            if "delete_after_epoch" not in workspace_columns:
                connection.execute(
                    text(
                        'ALTER TABLE "workspaces" '
                        'ADD COLUMN "delete_after_epoch" INTEGER NULL'
                    )
                )
            connection.execute(
                text(
                    'CREATE INDEX IF NOT EXISTS "ix_workspaces_delete_after_epoch" '
                    'ON "workspaces" ("delete_after_epoch")'
                )
            )
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, kind, seed_version, created_at) "
                    "VALUES ('personal', 'personal', 0, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (id) DO NOTHING"
                )
            )
