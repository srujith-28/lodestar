"""Keep integration tests isolated from the configured local application database."""

import pytest


@pytest.fixture(autouse=True)
def disable_application_database_startup(monkeypatch) -> None:
    from app import main

    monkeypatch.setattr(main, "initialize_database", lambda: None)
