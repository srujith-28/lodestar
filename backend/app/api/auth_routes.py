"""Login and session endpoints for the single-user demo."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.core.config import MAX_AUTH_PASSWORD_LENGTH, MIN_AUTH_PASSWORD_LENGTH, Settings, get_settings
from app.core.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    SessionClaims,
    constant_time_equal,
    new_csrf_token,
    read_session,
    issue_session,
    request_origin_is_allowed,
    require_authenticated,
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
    settings: Settings = Depends(get_settings),
) -> SessionResponse:
    _no_store(response)
    if not settings.auth_configured:
        return SessionResponse(configured=False, authenticated=False)
    claims = read_session(request.cookies.get(SESSION_COOKIE), settings)
    return SessionResponse(configured=True, authenticated=claims is not None)


@router.get("/csrf", response_model=CsrfResponse)
def csrf_token(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> CsrfResponse:
    _no_store(response)
    _require_auth_configuration(settings)
    if not request_origin_is_allowed(request, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request origin is not allowed.")

    claims = read_session(request.cookies.get(SESSION_COOKIE), settings)
    token = claims["csrf_token"] if claims is not None else new_csrf_token()
    _set_csrf_cookie(response, token, settings)
    return CsrfResponse(csrf_token=token)


@router.post("/login", response_model=LoginResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    csrf_header: str | None = Header(default=None, alias=CSRF_HEADER),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    _no_store(response)
    _require_auth_configuration(settings)
    if not request_origin_is_allowed(request, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request origin is not allowed.")
    csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
    if not csrf_header or not constant_time_equal(csrf_header, csrf_cookie):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed.")

    configured_password = settings.lodestar_auth_password.get_secret_value().encode("utf-8")
    submitted_password = payload.password.get_secret_value().encode("utf-8")
    if not hmac.compare_digest(submitted_password, configured_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect password.")

    rotated_csrf = new_csrf_token()
    _set_session_cookie(response, issue_session(rotated_csrf, settings), settings)
    _set_csrf_cookie(response, rotated_csrf, settings)
    return LoginResponse(authenticated=True, csrf_token=rotated_csrf)


@router.post("/logout")
def logout(
    response: Response,
    _: SessionClaims = Depends(require_authenticated),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    _no_store(response)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=settings.secure_cookies, httponly=True, samesite="lax")
    return {"logged_out": True}
