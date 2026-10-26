"""Nebius Token Factory chat service using its OpenAI-compatible API."""

from collections.abc import Sequence

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
        timeout=settings.nebius_timeout_seconds,
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
        async with create_provider_client(settings) as client:
            completion = await client.chat.completions.create(
                model=settings.nebius_model_id,
                messages=messages,
            )
    except APITimeoutError:
        raise ChatProviderError(504, "The Nebius request timed out. Please try again.") from None
    except AuthenticationError:
        raise ChatProviderError(
            502,
            "Nebius rejected the server-side credentials. Check the configured Token Factory key.",
        ) from None
    except RateLimitError:
        raise ChatProviderError(503, "Nebius is busy. Please try again shortly.") from None
    except APIConnectionError:
        raise ChatProviderError(502, "Could not reach the configured Nebius endpoint.") from None
    except APIStatusError:
        raise ChatProviderError(502, "Nebius could not complete the chat request.") from None
    except APIError:
        raise ChatProviderError(502, "The Nebius chat request failed.") from None

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
