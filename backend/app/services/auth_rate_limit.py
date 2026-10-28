"""Persistent, atomic login-attempt throttling for the single-user demo."""

from __future__ import annotations

import hmac
from ipaddress import ip_address
import time

from fastapi import Request
from sqlalchemy import case, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import keyed_identifier_hash
from app.models import AuthLoginRateLimit


class LoginRateLimiterUnavailable(RuntimeError):
    """The configured database cannot safely perform the rate-limit upsert."""


def login_client_key(request: Request, settings: Settings) -> str:
    """Hash the observed peer, trusting forwarding data only from configured proxies.

    Missing or invalid peer addresses share the ``unknown`` bucket. This is
    deliberately conservative: caller-supplied forwarding headers cannot bypass
    throttling, but per-client limiting behind a proxy requires an explicitly
    trusted proxy address and verified forwarding behavior.
    """
    client = request.client
    address = client.host.strip().lower() if client and client.host else "unknown"
    try:
        peer = ip_address(address)
    except ValueError:
        peer = None

    if peer is not None and any(peer in network for network in settings.trusted_proxy_networks):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            try:
                chain = [ip_address(item.strip()) for item in forwarded.split(",")]
            except ValueError:
                chain = []
            if chain:
                # Walk from the proxy-facing end and skip only explicitly trusted hops.
                address = chain[0].compressed
                for candidate in reversed(chain):
                    if any(candidate in network for network in settings.trusted_proxy_networks):
                        continue
                    address = candidate.compressed
                    break
    elif peer is not None:
        address = peer.compressed
    return keyed_identifier_hash(address, settings, purpose="login-client")


def reserve_login_attempt(
    db: Session,
    client_key_hash: str,
    settings: Settings,
    *,
    now: int | None = None,
    max_attempts: int | None = None,
    window_seconds: int | None = None,
) -> int | None:
    """Atomically reserve one attempt; return Retry-After seconds when blocked."""
    current_time = int(time.time()) if now is None else now
    window_seconds = window_seconds or settings.login_rate_limit_window_seconds
    max_attempts = max_attempts or settings.login_rate_limit_max_attempts
    if window_seconds < 1 or max_attempts < 1:
        raise ValueError("Rate-limit policy values must be positive.")
    bucket = AuthLoginRateLimit.__table__
    window_expired = bucket.c.window_started_at_epoch <= current_time - window_seconds

    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        insert = sqlite_insert(AuthLoginRateLimit)
    elif dialect == "postgresql":
        insert = postgresql_insert(AuthLoginRateLimit)
    else:
        raise LoginRateLimiterUnavailable("Unsupported database dialect for atomic login throttling.")

    try:
        # Prune stale client buckets so unique source addresses cannot grow the table forever.
        db.execute(
            delete(AuthLoginRateLimit).where(
                AuthLoginRateLimit.window_started_at_epoch
                <= current_time - (2 * window_seconds)
            )
        )
        statement = (
            insert.values(
                client_key_hash=client_key_hash,
                window_started_at_epoch=current_time,
                attempt_count=1,
            )
            .on_conflict_do_update(
                index_elements=[AuthLoginRateLimit.client_key_hash],
                set_={
                    "window_started_at_epoch": case(
                        (window_expired, current_time),
                        else_=bucket.c.window_started_at_epoch,
                    ),
                    "attempt_count": case(
                        (window_expired, 1),
                        else_=bucket.c.attempt_count + 1,
                    ),
                },
                where=or_(window_expired, bucket.c.attempt_count < max_attempts),
            )
            .returning(
                AuthLoginRateLimit.window_started_at_epoch,
                AuthLoginRateLimit.attempt_count,
            )
        )
        result = db.execute(statement).one_or_none()
        if result is not None:
            db.commit()
            return None

        window_start = db.execute(
            select(AuthLoginRateLimit.window_started_at_epoch).where(
                AuthLoginRateLimit.client_key_hash == client_key_hash
            )
        ).scalar_one_or_none()
        db.commit()
        if window_start is None:
            # Fail closed if an unexpected race removed the row while this request was blocked.
            return window_seconds
        return max(1, window_start + window_seconds - current_time)
    except SQLAlchemyError:
        db.rollback()
        raise


def clear_login_attempts(db: Session, client_key_hash: str) -> None:
    """Clear the successful caller's bucket within the surrounding login transaction."""
    db.execute(
        delete(AuthLoginRateLimit).where(AuthLoginRateLimit.client_key_hash == client_key_hash)
    )
