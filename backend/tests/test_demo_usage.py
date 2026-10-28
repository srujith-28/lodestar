"""Database-backed guest request and provider budgets."""

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database import Base
from app.services.demo_usage import (
    GuestLimitExceeded,
    reserve_guest_provider_call,
    reserve_guest_request,
)


@pytest.fixture
def usage_db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'usage.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_guest_request_limit_expires_and_is_per_workspace(usage_db) -> None:
    settings = Settings(
        _env_file=None,
        lodestar_session_secret=SecretStr("usage-test-session-secret-long-enough-123456"),
        guest_demo_enabled=True,
        guest_api_rate_limit_max_requests=10,
        guest_api_rate_limit_window_seconds=10,
    )
    for _ in range(10):
        reserve_guest_request(usage_db, "guest_a", settings, now=100)
    with pytest.raises(GuestLimitExceeded):
        reserve_guest_request(usage_db, "guest_a", settings, now=101)

    # A different signed workspace has its own API budget.
    reserve_guest_request(usage_db, "guest_b", settings, now=101)
    reserve_guest_request(usage_db, "guest_a", settings, now=110)


def test_guest_provider_calls_are_limited_per_workspace_and_globally(usage_db) -> None:
    settings = Settings(
        _env_file=None,
        lodestar_session_secret=SecretStr("usage-test-session-secret-long-enough-123456"),
        guest_demo_enabled=True,
        guest_provider_calls_per_workspace_per_day=1,
        guest_provider_calls_global_per_day=2,
    )
    reserve_guest_provider_call(usage_db, "guest_a", settings, now=1_000)
    with pytest.raises(GuestLimitExceeded):
        reserve_guest_provider_call(usage_db, "guest_a", settings, now=1_001)
    reserve_guest_provider_call(usage_db, "guest_b", settings, now=1_002)
    with pytest.raises(GuestLimitExceeded):
        reserve_guest_provider_call(usage_db, "guest_c", settings, now=1_003)

    # The fixed daily window rolls forward; no secret or request data is stored.
    reserve_guest_provider_call(usage_db, "guest_a", settings, now=87_400)
