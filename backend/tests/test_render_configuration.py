"""Keep the deployment Blueprint parseable and aligned with the app layout."""

from pathlib import Path

import yaml
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url

from app.core.config import Settings


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_render_blueprint_defines_one_web_service_and_persistent_database() -> None:
    blueprint = yaml.safe_load((REPOSITORY_ROOT / "render.yaml").read_text(encoding="utf-8"))

    services = blueprint["services"]
    databases = blueprint["databases"]
    assert len(services) == 1
    assert services[0]["runtime"] == "docker"
    assert services[0]["healthCheckPath"] == "/health"
    assert services[0]["envVars"]
    env_vars = {item["key"]: item for item in services[0]["envVars"]}
    database_url = next(item for item in services[0]["envVars"] if item["key"] == "DATABASE_URL")
    assert database_url["fromDatabase"]["property"] == "connectionString"
    assert env_vars["LOGIN_RATE_LIMIT_MAX_ATTEMPTS"]["value"] == "5"
    assert env_vars["LOGIN_RATE_LIMIT_WINDOW_SECONDS"]["value"] == "900"
    assert env_vars["GUEST_DEMO_ENABLED"]["value"] == "true"
    assert env_vars["GUEST_DATA_RETENTION_DAYS"]["value"] == "7"
    assert env_vars["GUEST_PROVIDER_CALLS_GLOBAL_PER_DAY"]["value"] == "40"
    # No Render proxy CIDRs are configured until the deployment trust boundary
    # and forwarding-header sanitization behavior have been verified.
    assert "TRUSTED_PROXY_CIDRS" not in env_vars
    assert len(databases) == 1
    assert databases[0]["plan"] == "free"

    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    # Keep the raw socket peer visible to app-level trusted-proxy validation;
    # Uvicorn must not rewrite request.client from untrusted forwarding headers.
    assert "--no-proxy-headers" in dockerfile


def test_staging_blueprint_uses_external_database_and_staging_branch() -> None:
    production = yaml.safe_load((REPOSITORY_ROOT / "render.yaml").read_text(encoding="utf-8"))
    staging_text = (REPOSITORY_ROOT / "render.staging.yaml").read_text(encoding="utf-8")
    staging = yaml.safe_load(staging_text)

    production_service = production["services"][0]
    production_database = production["databases"][0]
    service = staging["services"][0]
    env_vars = {item["key"]: item for item in service["envVars"]}

    assert service["name"] == "lodestar-staging"
    assert service["runtime"] == "docker"
    assert service["branch"] == "staging/guest-demo"
    assert service["autoDeployTrigger"] == "off"
    assert service["dockerfilePath"] == "./Dockerfile"
    assert service["dockerContext"] == "."
    assert service["healthCheckPath"] == "/health"
    assert service["name"] != production_service["name"]
    assert "databases" not in staging
    assert env_vars["DATABASE_URL"] == {"key": "DATABASE_URL", "sync": False}
    assert "fromDatabase" not in staging_text
    assert production_service["name"] not in staging_text
    assert production_database["name"] not in staging_text
    assert env_vars["CORS_ORIGINS"]["fromService"] == {
        "type": "web",
        "name": "lodestar-staging",
        "envVarKey": "RENDER_EXTERNAL_URL",
    }
    assert env_vars["LODESTAR_SESSION_SECRET"]["generateValue"] is True
    assert env_vars["LODESTAR_AUTH_PASSWORD"]["sync"] is False
    assert "NEBIUS_API_KEY" not in env_vars
    assert "TRUSTED_PROXY_CIDRS" not in env_vars


def test_neon_style_postgresql_url_keeps_ssl_query_parameters() -> None:
    url = "postgresql://lodestar_user:placeholder@ep-example.neon.tech/neondb?sslmode=require"
    settings = Settings(_env_file=None, database_url=url)

    assert settings.database_url.get_secret_value() == url
    parsed_url = make_url(settings.database_url.get_secret_value())
    dialect = postgresql.dialect()
    _, connect_parameters = dialect.create_connect_args(parsed_url)
    requirements = (REPOSITORY_ROOT / "backend" / "requirements.txt").read_text(
        encoding="utf-8"
    )

    assert dialect.name == "postgresql"
    assert dialect.driver == "psycopg2"
    assert connect_parameters["sslmode"] == "require"
    assert "psycopg2-binary==2.9.10" in requirements
