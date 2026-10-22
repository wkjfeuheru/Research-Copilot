"""健康接口契约测试。"""

from fastapi.testclient import TestClient

from research_copilot import __version__
from research_copilot.api.app import create_app
from research_copilot.api.schemas.health import CheckStatus, HealthResponse
from research_copilot.config import Settings


def make_client(**settings_overrides: object) -> TestClient:
    settings = Settings(environment="test", _env_file=None, **settings_overrides)
    return TestClient(create_app(settings))


def test_health_returns_explicit_process_only_contract() -> None:
    with make_client() as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")

    payload = HealthResponse.model_validate(response.json())
    assert payload.status == "ok"
    assert payload.service == "Research Copilot API"
    assert payload.version == __version__
    assert payload.environment == "test"
    assert payload.checked_at.tzinfo is not None
    assert "不表示研究能力就绪" in payload.message

    assert payload.checks.api.status is CheckStatus.OK
    assert payload.checks.database.status is CheckStatus.NOT_CONFIGURED
    assert payload.checks.model.status is CheckStatus.NOT_CONFIGURED
    assert "未配置研究模型密钥" in payload.checks.model.detail
    assert payload.checks.workflow.status is CheckStatus.NOT_CONFIGURED


def test_configured_dependencies_are_reported_as_unverified() -> None:
    with make_client(
        database_url="postgresql://user:pass@localhost/research",
        research_model="local-test-model",
        research_model_api_key="not-a-real-key",
    ) as client:
        response = client.get("/api/health")

    payload = HealthResponse.model_validate(response.json())
    assert payload.checks.database.status is CheckStatus.NOT_CHECKED
    assert payload.checks.model.status is CheckStatus.NOT_CHECKED
    assert "健康检查不发起模型调用" in payload.checks.model.detail
    assert payload.checks.workflow.status is CheckStatus.NOT_CONFIGURED
    assert "not-a-real-key" not in response.text


def test_health_does_not_expose_database_url_or_key() -> None:
    with make_client(
        database_url="postgresql://secret-user:secret-pass@db.internal:5432/research",
        research_model_api_key="secret-model-key",
    ) as client:
        response = client.get("/api/health")

    assert "secret-user" not in response.text
    assert "secret-pass" not in response.text
    assert "secret-model-key" not in response.text


def test_unknown_route_is_not_treated_as_health_success() -> None:
    with make_client() as client:
        response = client.get("/api/not-a-route")

    assert response.status_code == 404
