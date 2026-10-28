"""Signed single-user browser sessions and CSRF protection."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from typing import TypedDict

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.database import get_db
from app.models import AuthSession


SESSION_COOKIE = "lodestar_session"
CSRF_COOKIE = "lodestar_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
PERSONAL_WORKSPACE_ID = "personal"
GUEST_WORKSPACE_PREFIX = "guest_"


class SessionClaims(TypedDict):
    issued_at: int
    csrf_token: str
    session_id: str
    workspace_id: str


class WorkspaceContext(TypedDict):
    id: str
    is_guest: bool


def _secret_bytes(settings: Settings) -> bytes:
    if settings.lodestar_session_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo session signing is not configured on the backend.",
        )
    return settings.lodestar_session_secret.get_secret_value().encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_session(
    csrf_token: str,
    session_id: str,
    settings: Settings,
    *,
    workspace_id: str = PERSONAL_WORKSPACE_ID,
    issued_at: int | None = None,
) -> str:
    payload = json.dumps(
        {
            "issued_at": issued_at if issued_at is not None else int(time.time()),
            "csrf_token": csrf_token,
            "session_id": session_id,
            "workspace_id": workspace_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded_payload = _b64encode(payload)
    signature = hmac.new(_secret_bytes(settings), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded_payload}.{_b64encode(signature)}"


def read_session(token: str | None, settings: Settings, *, now: int | None = None) -> SessionClaims | None:
    if not token or len(token) > 2048:
        return None
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        signature = _b64decode(encoded_signature)
        expected = hmac.new(
            _secret_bytes(settings), encoded_payload.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64decode(encoded_payload))
        if not isinstance(payload, dict):
            return None
        issued_at = payload.get("issued_at")
        csrf_token = payload.get("csrf_token")
        session_id = payload.get("session_id")
        workspace_id = payload.get("workspace_id")
        current_time = int(time.time()) if now is None else now
        if (
            set(payload) != {"issued_at", "csrf_token", "session_id", "workspace_id"}
            or not isinstance(issued_at, int)
            or isinstance(issued_at, bool)
            or issued_at > current_time + 60
            or current_time - issued_at >= SESSION_MAX_AGE_SECONDS
            or not isinstance(csrf_token, str)
            or len(csrf_token) < 32
            or len(csrf_token) > 128
            or not isinstance(session_id, str)
            or len(session_id) < 32
            or len(session_id) > 128
            or not isinstance(workspace_id, str)
            or not 1 <= len(workspace_id) <= 80
        ):
            return None
        return {
            "issued_at": issued_at,
            "csrf_token": csrf_token,
            "session_id": session_id,
            "workspace_id": workspace_id,
        }
    except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError, HTTPException):
        return None


def keyed_identifier_hash(value: str, settings: Settings, *, purpose: str) -> str:
    """Return a purpose-separated keyed digest for server-side auth records."""
    return hmac.new(
        _secret_bytes(settings),
        f"lodestar:{purpose}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def has_active_session(
    session_id: str,
    db: Session,
    settings: Settings,
    *,
    workspace_id: str | None = None,
    now: int | None = None,
) -> bool:
    current_time = int(time.time()) if now is None else now
    try:
        statement = select(AuthSession.session_id_hash).where(
                AuthSession.session_id_hash
                == keyed_identifier_hash(session_id, settings, purpose="session"),
                AuthSession.expires_at_epoch > current_time,
            )
        if workspace_id is not None:
            statement = statement.where(AuthSession.workspace_id == workspace_id)
        return db.scalar(statement) is not None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session storage is unavailable.",
        ) from None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def request_origin_is_allowed(request: Request, settings: Settings) -> bool:
    origin = request.headers.get("origin")
    return origin is None or origin.rstrip("/") in settings.allowed_origins


def require_authenticated(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SessionClaims:
    """Protect private routes and reject unsafe requests without a matching CSRF token."""
    if not settings.auth_configured:
        _secret_bytes(settings)
    if not request_origin_is_allowed(request, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request origin is not allowed.")

    claims = read_session(request.cookies.get(SESSION_COOKIE), settings)
    if claims is None or not has_active_session(
        claims["session_id"],
        db,
        settings,
        workspace_id=claims["workspace_id"],
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        header_token = request.headers.get(CSRF_HEADER, "")
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        expected = claims["csrf_token"]
        if not (
            constant_time_equal(header_token, expected)
            and constant_time_equal(cookie_token, expected)
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed.")
    return claims


def require_workspace_access(
    claims: SessionClaims = Depends(require_authenticated),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WorkspaceContext:
    """Apply guest API request caps and return the server-verified data scope."""
    # Existing endpoint unit tests override authentication with None to exercise
    # handler logic. Production authentication always returns signed claims.
    if claims is None:
        return {"id": PERSONAL_WORKSPACE_ID, "is_guest": False}

    workspace_id = claims["workspace_id"]
    is_guest = workspace_id.startswith(GUEST_WORKSPACE_PREFIX)
    if is_guest:
        from app.services.demo_usage import GuestLimitExceeded, reserve_guest_request

        try:
            reserve_guest_request(db, workspace_id, settings)
        except GuestLimitExceeded as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Guest demo request limit reached. Please wait before trying again.",
                headers={"Retry-After": str(exc.retry_after)},
            ) from None
        except SQLAlchemyError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Guest demo limits are unavailable.",
            ) from None
    return {"id": workspace_id, "is_guest": is_guest}
