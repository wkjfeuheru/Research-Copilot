"""应用配置。

配置通过 ``RC_`` 前缀的环境变量或项目根目录的 ``.env`` 读取。应用不要求
数据库、模型密钥或搜索服务配置即可启动；这些配置存在也不代表服务已验证。
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]


class Settings(BaseSettings):
    """运行配置。

    字段默认值只保证骨架可启动，不声明任何外部依赖已经就绪。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="RC_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Research Copilot API"
    environment: Environment = "development"
    api_prefix: str = Field(default="/api")
    database_url: str | None = None

    # DeepSeek 当前官方模型名为 deepseek-flash / deepseek-v4-pro；
    # deepseek-chat 已被官方宣布于 2026-07-24 下线，不设为默认值。
    research_model: str = Field(default="deepseek-flash", min_length=1)
    research_model_base_url: str = Field(
        default="https://api.deepseek.com",
        min_length=1,
    )
    research_model_api_key: SecretStr | None = None
    research_model_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    research_model_max_tokens: int = Field(default=8192, ge=1, le=393216)

    @field_validator("api_prefix")
    @classmethod
    def normalize_api_prefix(cls, value: str) -> str:
        """接受 ``/api``、``/api/``，拒绝缺少前导斜杠的前缀。"""

        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("api_prefix 必须以 '/' 开头")
        return normalized.rstrip("/") or "/"

    @field_validator("research_model")
    @classmethod
    def normalize_research_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("research_model 不能为空")
        return normalized

    @field_validator("research_model_base_url")
    @classmethod
    def normalize_research_model_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("research_model_base_url 必须以 http:// 或 https:// 开头")
        return normalized

    @property
    def database_configured(self) -> bool:
        return bool(self.database_url)

    @property
    def research_model_configured(self) -> bool:
        key = self.research_model_api_key
        return bool(
            self.research_model
            and key is not None
            and key.get_secret_value().strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程级配置缓存。测试需要独立配置时应直接构造 ``Settings``。"""

    return Settings()


__all__ = ["Environment", "Settings", "get_settings"]
