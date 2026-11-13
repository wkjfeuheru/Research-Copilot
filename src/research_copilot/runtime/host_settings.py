"""Host configuration read from the environment. This is not the first-version Settings."""

from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def find_project_root(start: Path | str | None = None) -> Path:
    current = Path(start) if start is not None else Path.cwd()
    try:
        current = current.expanduser().resolve()
    except OSError:
        current = Path(current).absolute()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return current


_PROJECT_ROOT = find_project_root(Path(__file__).resolve().parent)
_ENV_FILE = _PROJECT_ROOT / ".env"


class HostSettings(BaseSettings):
    """Credentials and database URL for the session host."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        env_prefix="RC_",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = Field(default="https://api.deepseek.com", min_length=1)
    financekb_base_url: str | None = None
    financekb_api_token: SecretStr | None = Field(default=None, repr=False)
    financekb_ca_bundle: str | None = Field(default=None, repr=False)
    financekb_call_timeout_seconds: float = Field(default=150.0, ge=1.0, le=600.0)
    database_url: str | None = Field(default=None, repr=False)

    @field_validator("deepseek_api_key", "financekb_api_token", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: Any) -> Any:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("financekb_base_url", mode="before")
    @classmethod
    def _financekb_origin(cls, value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        if not text:
            return None
        address = urlsplit(text)
        if (
            address.scheme != "https"
            or not address.hostname
            or address.username is not None
            or address.password is not None
            or address.path not in ("", "/")
            or "?" in text
            or "#" in text
            or any(ch.isspace() for ch in text)
        ):
            raise ValueError("RC_FINANCEKB_BASE_URL must be an HTTPS origin")
        return text.rstrip("/")

    @field_validator("financekb_ca_bundle", mode="before")
    @classmethod
    def _financekb_ca_file(cls, value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        if not text:
            return None
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = _PROJECT_ROOT / path
        try:
            path = path.resolve()
        except OSError:
            raise ValueError("RC_FINANCEKB_CA_BUNDLE must be a readable file") from None
        if not path.is_file():
            raise ValueError("RC_FINANCEKB_CA_BUNDLE must be a readable file")
        return str(path)

    @model_validator(mode="after")
    def _financekb_settings_pair(self) -> Self:
        if bool(self.financekb_base_url) != bool(self.financekb_api_token):
            raise ValueError(
                "Set RC_FINANCEKB_BASE_URL and RC_FINANCEKB_API_TOKEN together"
            )
        if self.financekb_ca_bundle and not self.financekb_base_url:
            raise ValueError(
                "RC_FINANCEKB_CA_BUNDLE requires RC_FINANCEKB_BASE_URL"
            )
        if self.financekb_api_token:
            token = self.financekb_api_token.get_secret_value()
            if (
                not token.isascii()
                or not token.isprintable()
                or any(ch.isspace() for ch in token)
            ):
                raise ValueError("RC_FINANCEKB_API_TOKEN must be a valid Bearer token")
        return self

    @field_validator("database_url", mode="before")
    @classmethod
    def _validate_database_url(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if not text.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError(
                "RC_DATABASE_URL 必须使用 postgresql:// 或 postgresql+psycopg:// 方案"
            )
        return text

    @field_validator("deepseek_base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("RC_DEEPSEEK_BASE_URL 必须以 http:// 或 https:// 开头")
        return normalized

    @property
    def deepseek_configured(self) -> bool:
        return self.deepseek_api_key is not None

    @property
    def financekb_configured(self) -> bool:
        return self.financekb_base_url is not None and self.financekb_api_token is not None
