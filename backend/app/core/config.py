"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
MIN_AUTH_PASSWORD_LENGTH = 12
MAX_AUTH_PASSWORD_LENGTH = 256


class Settings(BaseSettings):
    """Settings are read from process environment variables and the root .env file."""

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Lodestar API"
    app_env: str = "development"
    database_url: str = "sqlite:///./lodestar.db"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Single-user demo authentication. Both values must be configured together.
    lodestar_auth_password: SecretStr | None = None
    lodestar_session_secret: SecretStr | None = None

    # The key stays server-side and is masked in settings representations.
    nebius_api_key: SecretStr | None = None
    nebius_base_url: str = DEFAULT_NEBIUS_BASE_URL
    nebius_model_id: str | None = None
    nebius_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    tavily_api_key: SecretStr | None = None
    tavily_base_url: str | None = None

    @field_validator("app_env", mode="before")
    @classmethod
    def normalize_app_env(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("lodestar_auth_password", "lodestar_session_secret", mode="before")
    @classmethod
    def normalize_auth_secrets(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("lodestar_auth_password")
    @classmethod
    def validate_auth_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_AUTH_PASSWORD_LENGTH:
            raise ValueError(
                f"LODESTAR_AUTH_PASSWORD must contain at least {MIN_AUTH_PASSWORD_LENGTH} characters."
            )
        if value is not None and len(value.get_secret_value()) > MAX_AUTH_PASSWORD_LENGTH:
            raise ValueError(
                f"LODESTAR_AUTH_PASSWORD must contain no more than {MAX_AUTH_PASSWORD_LENGTH} characters."
            )
        return value

    @field_validator("lodestar_session_secret")
    @classmethod
    def validate_session_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("LODESTAR_SESSION_SECRET must contain at least 32 characters.")
        return value

    @model_validator(mode="after")
    def validate_auth_configuration(self) -> "Settings":
        if self.app_env not in {"development", "production"}:
            raise ValueError("APP_ENV must be either 'development' or 'production'.")
        has_password = self.lodestar_auth_password is not None
        has_secret = self.lodestar_session_secret is not None
        if has_password != has_secret:
            raise ValueError(
                "LODESTAR_AUTH_PASSWORD and LODESTAR_SESSION_SECRET must be configured together."
            )
        if self.app_env == "production" and not (has_password and has_secret):
            raise ValueError(
                "Production requires LODESTAR_AUTH_PASSWORD and LODESTAR_SESSION_SECRET."
            )
        if self.app_env == "production" and any(
            urlsplit(origin).scheme != "https" for origin in self.allowed_origins
        ):
            raise ValueError("Production CORS_ORIGINS must use HTTPS origins.")
        return self

    @property
    def auth_configured(self) -> bool:
        return self.lodestar_auth_password is not None and self.lodestar_session_secret is not None

    @property
    def secure_cookies(self) -> bool:
        return self.app_env == "production"

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
