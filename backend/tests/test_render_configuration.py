"""Keep the deployment Blueprint parseable and aligned with the app layout."""

from pathlib import Path

import yaml


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
    # No Render proxy CIDRs are configured until the deployment trust boundary
    # and forwarding-header sanitization behavior have been verified.
    assert "TRUSTED_PROXY_CIDRS" not in env_vars
    assert len(databases) == 1
    assert databases[0]["plan"] == "free"
