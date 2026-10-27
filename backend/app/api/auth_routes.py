"""Login and session endpoints for the single-user demo."""

from __future__ import annotations

import hmac
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import MAX_AUTH_PASSWORD_LENGTH, MIN_AUTH_PASSWORD_LENGTH, Settings, get_settings
from app.database import get_db
from app.core.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    SessionClaims,
    constant_time_equal,
    has_active_session,
    keyed_identifier_hash,
    new_csrf_token,
    read_session,
    issue_session,
    request_origin_is_allowed,
    require_authenticated,
)
from app.models import AuthSession
from app.services.auth_rate_limit import (
    LoginRateLimiterUnavailable,
    clear_login_attempts,
    login_client_key,
    reserve_login_attempt,
)


router = APIRouter(prefix="/api/auth", tags=["authentication"])


class SessionResponse(BaseModel):
    configured: bool
    authenticated: bool


class CsrfResponse(BaseModel):
    csrf_token: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False)

    password: SecretStr = Field(min_length=MIN_AUTH_PASSWORD_LENGTH, max_length=MAX_AUTH_PASSWORD_LENGTH)


class LoginResponse(BaseModel):
    authenticated: bool
    csrf_token: str


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _require_auth_configuration(settings: Settings) -> None:
    if not settings.auth_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo authentication is not configured. Set LODESTAR_AUTH_PASSWORD and LODESTAR_SESSION_SECRET.",
        )


def _database_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication storage is unavailable.",
    )


def _set_csrf_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )


@router.get("/session", response_model=SessionResponse)
def session_status(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SessionResponse:
    _no_store(response)
    if not settings.auth_configured:
        return SessionResponse(configured=False, authenticated=False)
    claims = read_session(request.cookies.get(SESSION_COOKIE), settings)
    authenticated = bool(
        claims is not None and has_active_session(claims["session_id"], db, settings)
    )
    return SessionResponse(configured=True, authenticated=authenticated)


@router.get("/csrf", response_model=CsrfResponse)
def csrf_token(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CsrfResponse:
    _no_store(response)
    _require_auth_configuration(settings)
    if not request_origin_is_allowed(request, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request origin is not allowed.")

    claims = read_session(request.cookies.get(SESSION_COOKIE), settings)
    if claims is not None and not has_active_session(claims["session_id"], db, settings):
        claims = None
    token = claims["csrf_token"] if claims is not None else new_csrf_token()
    _set_csrf_cookie(response, token, settings)
    return CsrfResponse(csrf_token=token)


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    csrf_header: str | None = Header(default=None, alias=CSRF_HEADER),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    _no_store(response)
    _require_auth_configuration(settings)
    if not request_origin_is_allowed(request, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request origin is not allowed.")
    csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
    if not csrf_header or not constant_time_equal(csrf_header, csrf_cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed.")

    client_key_hash = login_client_key(request, settings)
    try:
        retry_after = reserve_login_attempt(db, client_key_hash, settings)
    except LoginRateLimiterUnavailable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Login rate limiting is unavailable for the configured database.",
        ) from None
    except SQLAlchemyError:
        raise _database_unavailable() from None
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many sign-in attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    configured_password = settings.lodestar_auth_password.get_secret_value().encode("utf-8")
    submitted_password = payload.password.get_secret_value().encode("utf-8")
    if not hmac.compare_digest(submitted_password, configured_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password.")

    rotated_csrf = new_csrf_token()
    session_id = secrets.token_urlsafe(32)
    now = int(time.time())
    try:
        db.execute(
            delete(AuthSession).where(AuthSession.expires_at_epoch <= now)
        )
        clear_login_attempts(db, client_key_hash)
        db.add(
            AuthSession(
                session_id_hash=keyed_identifier_hash(session_id, settings, purpose="session"),
                expires_at_epoch=now + SESSION_MAX_AGE_SECONDS,
            )
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise _database_unavailable() from None

    _set_session_cookie(
        response,
        issue_session(rotated_csrf, session_id, settings, issued_at=now),
        settings,
    )
    _set_csrf_cookie(response, rotated_csrf, settings)
    return LoginResponse(authenticated=True, csrf_token=rotated_csrf)


@router.post("/logout")
def logout(
    response: Response,
    claims: SessionClaims = Depends(require_authenticated),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    _no_store(response)
    try:
        db.execute(
            delete(AuthSession).where(
                AuthSession.session_id_hash
                == keyed_identifier_hash(claims["session_id"], settings, purpose="session")
            )
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise _database_unavailable() from None

    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax")
    return {"logged_out": True}
