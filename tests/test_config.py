"""配置边界测试：骨架启动不依赖密钥或外部服务。"""

import pytest
from pydantic import ValidationError

from research_copilot.config import Settings


def test_defaults_require_no_database_or_model_secrets() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "Research Copilot API"
    assert settings.environment == "development"
    assert settings.api_prefix == "/api"
    assert settings.database_url is None
    assert settings.research_model == "deepseek-flash"
    assert settings.research_model_base_url == "https://api.deepseek.com"
    assert settings.research_model_timeout_seconds == 60.0
    assert settings.research_model_max_tokens == 8192
    assert settings.research_model_api_key is None
    assert settings.database_configured is False
    assert settings.research_model_configured is False


def test_environment_variables_override_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RC_ENVIRONMENT", "test")
    monkeypatch.setenv("RC_DATABASE_URL", "postgresql://user:pass@localhost/research")
    monkeypatch.setenv("RC_RESEARCH_MODEL", "deepseek-chat")
    monkeypatch.setenv("RC_RESEARCH_MODEL_API_KEY", "not-a-real-key")
    monkeypatch.setenv("RC_RESEARCH_MODEL_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("RC_RESEARCH_MODEL_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("RC_RESEARCH_MODEL_MAX_TOKENS", "2048")

    settings = Settings(_env_file=None)

    assert settings.environment == "test"
    assert settings.database_configured is True
    assert settings.research_model == "deepseek-chat"
    assert settings.research_model_base_url == "https://example.test/v1"
    assert settings.research_model_timeout_seconds == 30.0
    assert settings.research_model_max_tokens == 2048
    assert settings.research_model_configured is True
    assert settings.research_model_api_key is not None
    assert "not-a-real-key" not in repr(settings)
    assert settings.research_model_api_key.get_secret_value() == "not-a-real-key"


def test_empty_api_key_does_not_mark_model_configured() -> None:
    settings = Settings(research_model_api_key="", _env_file=None)

    assert settings.research_model_api_key is not None
    assert settings.research_model_configured is False


def test_invalid_model_settings_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(research_model_base_url="api.deepseek.com", _env_file=None)

    with pytest.raises(ValidationError):
        Settings(research_model_timeout_seconds=0, _env_file=None)

    with pytest.raises(ValidationError):
        Settings(research_model_max_tokens=0, _env_file=None)

    with pytest.raises(ValidationError):
        Settings(research_model="   ", _env_file=None)


def test_api_prefix_is_normalized() -> None:
    assert Settings(api_prefix="/api/", _env_file=None).api_prefix == "/api"


def test_api_prefix_requires_leading_slash() -> None:
    with pytest.raises(ValidationError):
        Settings(api_prefix="api", _env_file=None)


def test_unknown_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RC_ENVIRONMENT", "staging")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
