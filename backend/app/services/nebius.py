"""Nebius Token Factory chat service using its OpenAI-compatible API."""

import asyncio
from collections.abc import Sequence

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from app.api.schemas import ChatMessage
from app.core.config import Settings


class ChatProviderError(Exception):
    """An error that can be returned without exposing upstream details."""

    def __init__(self, status_code: int, public_message: str) -> None:
        super().__init__(public_message)
        self.status_code = status_code
        self.public_message = public_message


def create_provider_client(settings: Settings) -> AsyncOpenAI:
    """Create an SDK client from server-side configuration."""
    api_key = settings.nebius_api_key
    if api_key is None:
        raise ChatProviderError(
            503,
            "Nebius chat is not configured. Set NEBIUS_API_KEY and a verified NEBIUS_MODEL_ID on the server.",
        )
    return AsyncOpenAI(
        api_key=api_key.get_secret_value(),
        base_url=settings.nebius_base_url,
        timeout=httpx.Timeout(
            connect=settings.nebius_connect_timeout_seconds,
            read=settings.nebius_timeout_seconds,
            write=settings.nebius_timeout_seconds,
            pool=settings.nebius_connect_timeout_seconds,
        ),
        max_retries=0,
    )


def _ensure_provider_configured(settings: Settings) -> None:
    api_key = settings.nebius_api_key
    if (
        api_key is None
        or not api_key.get_secret_value().strip()
        or not settings.nebius_model_id
    ):
        raise ChatProviderError(
            503,
            "Nebius chat is not configured. Set NEBIUS_API_KEY and a verified NEBIUS_MODEL_ID on the server.",
        )


def _safe_provider_status_message(status_code: int) -> str:
    """Map upstream status codes to actionable text without exposing response bodies."""
    if status_code == 402:
        return (
            "Nebius reported an account balance or billing issue (HTTP 402). "
            "Check the Token Factory account balance and billing settings."
        )
    if status_code == 403:
        return (
            "Nebius denied access to the configured model (HTTP 403). "
            "Check the API key permissions and model access."
        )
    if status_code == 404:
        return (
            "Nebius could not find the configured model or endpoint (HTTP 404). "
            "Verify NEBIUS_MODEL_ID and NEBIUS_BASE_URL."
        )
    if status_code == 408:
        return "Nebius timed out the request (HTTP 408). Please try again."
    if status_code == 409:
        return (
            "Nebius reported a capacity or model configuration conflict (HTTP 409). "
            "Check model availability and try again."
        )
    if status_code == 422:
        return (
            "Nebius rejected the request format (HTTP 422). "
            "Check that the configured model supports this request."
        )
    if status_code == 429:
        return (
            "Nebius rate limit or account quota reached (HTTP 429). "
            "Check Token Factory usage limits and account balance, then retry."
        )
    if status_code >= 500:
        return f"Nebius service error (HTTP {status_code}). Please try again later."
    return f"Nebius rejected the request (HTTP {status_code}). Check the configured model and request."


async def generate_reply(messages: Sequence[ChatMessage], settings: Settings) -> str:
    """Return a provider response; never synthesize a local fallback reply."""
    request_messages = [
        {"role": message.role, "content": message.content}
        for message in messages
    ]
    return await _generate_text(request_messages, settings)


async def generate_planner_completion(
    system_prompt: str,
    user_prompt: str,
    settings: Settings,
) -> str:
    """Ask the configured Nebius model for text; planning is validated separately."""
    return await _generate_text(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        settings,
    )


async def _generate_text(messages: list[dict[str, str]], settings: Settings) -> str:
    _ensure_provider_configured(settings)

    try:
        async def request_completion() -> object:
            async with create_provider_client(settings) as client:
                return await client.chat.completions.create(
                    model=settings.nebius_model_id,
                    messages=messages,
                )

        completion = await asyncio.wait_for(
            request_completion(),
            timeout=settings.nebius_timeout_seconds,
        )
    except (APITimeoutError, httpx.TimeoutException, TimeoutError):
        raise ChatProviderError(504, "The Nebius request timed out. Please try again.") from None
    except AuthenticationError:
        raise ChatProviderError(
            502,
            "Nebius rejected the server-side credentials (HTTP 401). Check that the Token Factory key is active.",
        ) from None
    except RateLimitError:
        raise ChatProviderError(503, _safe_provider_status_message(429)) from None
    except APIConnectionError:
        raise ChatProviderError(502, "Could not reach the configured Nebius endpoint.") from None
    except APIStatusError as exc:
        raise ChatProviderError(502, _safe_provider_status_message(exc.status_code)) from None
    except APIError:
        raise ChatProviderError(502, "The Nebius chat request failed.") from None
    except Exception:
        # Do not let unexpected SDK/transport details reach UI errors or server logs.
        raise ChatProviderError(502, "The Nebius chat request failed. Please try again.") from None

    return _extract_reply(completion)


def _extract_reply(completion: object) -> str:
    """Accept only the text shape expected from a chat-completions response."""
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, (list, tuple)) or not choices:
        raise ChatProviderError(502, "Nebius returned no chat response.")

    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    if not isinstance(content, str) or not content.strip():
        raise ChatProviderError(502, "Nebius returned no text response.")

    return content.strip()
