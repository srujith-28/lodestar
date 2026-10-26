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

from app.core.config import Settings, get_settings


SESSION_COOKIE = "lodestar_session"
CSRF_COOKIE = "lodestar_csrf"
CSRF_HEADER = "X-CSRF-Token"
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60


class SessionClaims(TypedDict):
    issued_at: int
    csrf_token: str


def _secret_bytes(settings: Settings) -> bytes:
    if not settings.auth_configured or settings.lodestar_session_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Demo authentication is not configured. Set LODESTAR_AUTH_PASSWORD and LODESTAR_SESSION_SECRET.",
        )
    return settings.lodestar_session_secret.get_secret_value().encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_session(csrf_token: str, settings: Settings, *, issued_at: int | None = None) -> str:
    payload = json.dumps(
        {"issued_at": issued_at if issued_at is not None else int(time.time()), "csrf_token": csrf_token},
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
        current_time = int(time.time()) if now is None else now
        if (
            not isinstance(issued_at, int)
            or issued_at > current_time + 60
            or current_time - issued_at > SESSION_MAX_AGE_SECONDS
            or not isinstance(csrf_token, str)
            or len(csrf_token) < 32
            or len(csrf_token) > 128
        ):
            return None
        return {"issued_at": issued_at, "csrf_token": csrf_token}
    except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError, HTTPException):
        return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def request_origin_is_allowed(request: Request, settings: Settings) -> bool:
    origin = request.headers.get("origin")
    return origin is None or origin.rstrip("/") in settings.allowed_origins


def require_authenticated(request: Request, settings: Settings = Depends(get_settings)) -> SessionClaims:
    """Protect private routes and reject unsafe requests without a matching CSRF token."""
    if not settings.auth_configured:
        _secret_bytes(settings)
    if not request_origin_is_allowed(request, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request origin is not allowed.")

    claims = read_session(request.cookies.get(SESSION_COOKIE), settings)
    if claims is None:
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
