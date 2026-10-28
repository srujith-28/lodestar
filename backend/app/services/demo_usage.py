"""Database-backed request and provider-call caps for public guest workspaces."""

from __future__ import annotations

import time

from sqlalchemy import case, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import GuestUsage


class GuestLimitExceeded(RuntimeError):
    """A guest request or provider-call quota has been reached."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Guest demo usage limit reached.")
        self.retry_after = max(1, retry_after)


def _insert_for(db: Session):
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite_insert(GuestUsage)
    if dialect == "postgresql":
        return postgresql_insert(GuestUsage)
    raise SQLAlchemyError("Guest limits require SQLite or PostgreSQL.")


def _reserve_request(
    db: Session,
    scope_key: str,
    *,
    now: int,
    window_seconds: int,
    max_requests: int,
) -> None:
    table = GuestUsage.__table__
    expired = table.c.request_window_started_at_epoch <= now - window_seconds
    statement = (
        _insert_for(db)
        .values(
            scope_key=scope_key,
            request_window_started_at_epoch=now,
            request_count=1,
            provider_window_started_at_epoch=now,
            provider_call_count=0,
        )
        .on_conflict_do_update(
            index_elements=[GuestUsage.scope_key],
            set_={
                "request_window_started_at_epoch": case(
                    (expired, now), else_=table.c.request_window_started_at_epoch
                ),
                "request_count": case(
                    (expired, 1), else_=table.c.request_count + 1
                ),
            },
            where=or_(expired, table.c.request_count < max_requests),
        )
        .returning(GuestUsage.request_window_started_at_epoch)
    )
    result = db.execute(statement).scalar_one_or_none()
    if result is not None:
        return
    started = db.scalar(
        select(GuestUsage.request_window_started_at_epoch).where(
            GuestUsage.scope_key == scope_key
        )
    )
    raise GuestLimitExceeded(
        window_seconds if started is None else started + window_seconds - now
    )


def reserve_guest_request(
    db: Session,
    workspace_id: str,
    settings: Settings,
    *,
    now: int | None = None,
) -> None:
    current_time = int(time.time()) if now is None else now
    try:
        _reserve_request(
            db,
            workspace_id,
            now=current_time,
            window_seconds=settings.guest_api_rate_limit_window_seconds,
            max_requests=settings.guest_api_rate_limit_max_requests,
        )
        db.commit()
    except GuestLimitExceeded:
        db.commit()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise


def _reserve_provider_call(
    db: Session,
    scope_key: str,
    *,
    now: int,
    max_calls: int,
) -> None:
    if max_calls <= 0:
        raise GuestLimitExceeded(86_400)
    table = GuestUsage.__table__
    window_seconds = 86_400
    expired = table.c.provider_window_started_at_epoch <= now - window_seconds
    statement = (
        _insert_for(db)
        .values(
            scope_key=scope_key,
            request_window_started_at_epoch=now,
            request_count=0,
            provider_window_started_at_epoch=now,
            provider_call_count=1,
        )
        .on_conflict_do_update(
            index_elements=[GuestUsage.scope_key],
            set_={
                "provider_window_started_at_epoch": case(
                    (expired, now), else_=table.c.provider_window_started_at_epoch
                ),
                "provider_call_count": case(
                    (expired, 1), else_=table.c.provider_call_count + 1
                ),
            },
            where=or_(expired, table.c.provider_call_count < max_calls),
        )
        .returning(GuestUsage.provider_window_started_at_epoch)
    )
    result = db.execute(statement).scalar_one_or_none()
    if result is not None:
        return
    started = db.scalar(
        select(GuestUsage.provider_window_started_at_epoch).where(
            GuestUsage.scope_key == scope_key
        )
    )
    raise GuestLimitExceeded(
        window_seconds if started is None else started + window_seconds - now
    )


def reserve_guest_provider_call(
    db: Session,
    workspace_id: str,
    settings: Settings,
    *,
    now: int | None = None,
) -> None:
    """Reserve both workspace and shared daily capacity before a paid model call."""
    current_time = int(time.time()) if now is None else now
    try:
        _reserve_provider_call(
            db,
            "global_provider_calls",
            now=current_time,
            max_calls=settings.guest_provider_calls_global_per_day,
        )
        _reserve_provider_call(
            db,
            workspace_id,
            now=current_time,
            max_calls=settings.guest_provider_calls_per_workspace_per_day,
        )
        db.commit()
    except GuestLimitExceeded:
        # Do not consume the shared budget when the workspace budget already blocks.
        db.rollback()
        raise
    except SQLAlchemyError:
        db.rollback()
        raise
