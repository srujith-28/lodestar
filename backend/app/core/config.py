"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/"


class Settings(BaseSettings):
    """Settings are read from process environment variables and the root .env file."""

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Lodestar API"
    database_url: str = "sqlite:///./lodestar.db"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # The key stays server-side and is masked in settings representations.
    nebius_api_key: SecretStr | None = None
    nebius_base_url: str = DEFAULT_NEBIUS_BASE_URL
    nebius_model_id: str | None = None
    nebius_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    tavily_api_key: SecretStr | None = None
    tavily_base_url: str | None = None

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, value: str) -> str:
        origins = [origin.strip() for origin in value.split(",") if origin.strip()]
        if not origins:
            raise ValueError("CORS_ORIGINS must contain at least one explicit browser origin.")

        for origin in origins:
            parsed = urlsplit(origin)
            try:
                port = parsed.port
            except ValueError:
                port = -1
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or port == -1
            ):
                raise ValueError(
                    "CORS_ORIGINS must list explicit HTTP or HTTPS origins without paths or credentials."
                )

        return ",".join(origins)

    @field_validator("nebius_api_key", mode="before")
    @classmethod
    def normalize_nebius_api_key(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("nebius_model_id", mode="before")
    @classmethod
    def normalize_nebius_model_id(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("nebius_base_url")
    @classmethod
    def validate_nebius_base_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return DEFAULT_NEBIUS_BASE_URL

        parsed = urlsplit(normalized)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "NEBIUS_BASE_URL must be an HTTPS URL without embedded credentials, query, or fragment."
            )

        return normalized.rstrip("/") + "/"

    @property
    def allowed_origins(self) -> list[str]:
        """Return the comma-separated browser origins as a normalized list."""
        return [origin.rstrip("/") for origin in self.cors_origins.split(",") if origin]


@lru_cache
def get_settings() -> Settings:
    """Return one settings instance for the running process."""
    return Settings()
