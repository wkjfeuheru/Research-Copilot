"""Explicit provider connections and per-role model bindings."""

import os
import tomllib
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, model_validator

from research_copilot.contracts import ROLE_NAMES
from research_copilot.llm.sdk import OpenAICompatibleChatModel


class ProviderConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(pattern=r"^https?://")
    api_key_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    timeout_seconds: float = Field(default=120, gt=0)


class RoleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str
    model: str = Field(min_length=1)
    context_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    tool_calling: bool
    image_inputs: bool = False
    output_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    reasoning_field: Literal["reasoning_content"] | None = None
    required_tool_choice: Literal["required", "auto"] = "required"
    temperature: float | None = None
    extra_body: dict = Field(default_factory=dict)
    context_chars_per_token: float = Field(default=2.0, ge=1, le=4)


class ModelsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: dict[str, ProviderConnection]
    roles: dict[str, RoleModel]
    summarizer: RoleModel | None = None

    @model_validator(mode="after")
    def validate_roles(self):
        if set(self.roles) != {"supervisor", *ROLE_NAMES}:
            raise ValueError("Exactly supervisor and the four role models are required")
        bindings = {
            **self.roles,
            **({"summarizer": self.summarizer} if self.summarizer else {}),
        }
        for name, role in bindings.items():
            if role.provider_id not in self.providers:
                raise ValueError("Unknown provider binding")
            if name != "summarizer" and not role.tool_calling:
                raise ValueError("All role models must support tool calling")
            if role.max_output_tokens >= role.context_tokens:
                raise ValueError("Output capacity must be below context capacity")
        return self


def load_models_config(path: Path) -> ModelsConfig:
    return ModelsConfig.model_validate(tomllib.loads(path.read_text("utf-8")))


@asynccontextmanager
async def open_models(
    config: ModelsConfig, *, credentials: Mapping[str, str] | None = None
):
    # Client lifetime is owned by the application, never by a report checkpoint.
    # https://github.com/openai/openai-python#async-usage
    async with AsyncExitStack() as stack:
        clients = {}
        bindings = {
            **config.roles,
            **({"summarizer": config.summarizer} if config.summarizer else {}),
        }
        for name in {role.provider_id for role in bindings.values()}:
            provider = config.providers[name]
            key = (credentials or {}).get(provider.api_key_env) or os.environ.get(
                provider.api_key_env
            )
            if not key:
                raise ValueError(
                    f"Missing model credential environment variable: {provider.api_key_env}"
                )
            clients[name] = await stack.enter_async_context(
                AsyncOpenAI(
                    base_url=provider.base_url,
                    api_key=key,
                    timeout=provider.timeout_seconds,
                    max_retries=0,
                )
            )
        yield {
            name: OpenAICompatibleChatModel(
                client=clients[role.provider_id],
                provider_id=role.provider_id,
                model_name=role.model,
                profile={
                    "max_input_tokens": role.context_tokens,
                    "max_output_tokens": role.max_output_tokens,
                    "tool_calling": role.tool_calling,
                    "image_inputs": role.image_inputs,
                },
                output_parameter=role.output_parameter,
                reasoning_field=role.reasoning_field,
                required_tool_choice=role.required_tool_choice,
                temperature=role.temperature,
                extra_body=role.extra_body,
                context_chars_per_token=role.context_chars_per_token,
            )
            for name, role in bindings.items()
        }
