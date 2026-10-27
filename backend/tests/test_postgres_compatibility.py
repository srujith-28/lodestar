"""PostgreSQL dialect compatibility checks without requiring a live database."""

import app.models  # noqa: F401 - registers all tables with Base.metadata
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.database import Base


def test_all_application_tables_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()

    assert Base.metadata.sorted_tables
    for table in Base.metadata.sorted_tables:
        compiled = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table.name}" in compiled
