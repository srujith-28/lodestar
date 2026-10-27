"""Health and service status endpoints."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.schemas import ChatRequest, ChatResponse
from app.core.config import Settings, get_settings
from app.core.security import require_authenticated
from app.database import engine
from app.services.nebius import ChatProviderError, generate_reply


router = APIRouter()
api_router = APIRouter(prefix="/api", dependencies=[Depends(require_authenticated)])


class HealthResponse(BaseModel):
    status: str
    service: str
    database: str


class ProviderStatusResponse(BaseModel):
    """Expose the configured provider/model state without exposing credentials."""

    provider: Literal["Nebius Token Factory"] = "Nebius Token Factory"
    status: Literal["missing_configuration", "configured_unverified"]
    api_key_configured: bool
    model_id_configured: bool
    model_id: str | None


@router.get("/health", response_model=HealthResponse, tags=["health"])
def health_check() -> HealthResponse:
    """Report API health only after successfully checking the configured database."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The configured database is unavailable.",
        ) from exc

    return HealthResponse(
        status="ok",
        service=get_settings().app_name,
        database="connected",
    )


@api_router.post("/chat", response_model=ChatResponse, tags=["chat"])
async def chat(
    request: ChatRequest,
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    """Forward validated chat history to the configured Nebius model."""
    try:
        reply = await generate_reply(request.messages, settings)
    except ChatProviderError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.public_message,
        ) from None

    return ChatResponse(reply=reply)


@api_router.get("/provider/status", response_model=ProviderStatusResponse, tags=["chat"])
def provider_status(settings: Settings = Depends(get_settings)) -> ProviderStatusResponse:
    """Report configuration only; this endpoint does not probe live inference."""
    api_key_configured = bool(
        settings.nebius_api_key
        and settings.nebius_api_key.get_secret_value().strip()
    )
    model_id_configured = bool(settings.nebius_model_id)
    return ProviderStatusResponse(
        status=(
            "configured_unverified"
            if api_key_configured and model_id_configured
            else "missing_configuration"
        ),
        api_key_configured=api_key_configured,
        model_id_configured=model_id_configured,
        model_id=settings.nebius_model_id,
    )
