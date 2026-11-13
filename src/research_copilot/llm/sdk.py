"""OpenAI SDK Chat Completions adapter; no agent loop, retries or fallback.

This version returns complete responses, including from astream's standard
LangChain fallback. It does not simulate token chunks. Protocol/error classes
remain those of the official SDK: https://github.com/openai/openai-python
"""

import json
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, model_validator


def _tool_images_as_user_content(wire):
    # Chat Completions accepts image parts in user messages. Keep the complete
    # parallel tool-response block together, then deliver its images as tool data.
    # https://api-docs.deepseek.com/guides/vision/#restrictions
    output, pictures = [], []
    for message in wire:
        if message["role"] != "tool" and pictures:
            output.append({"role": "user", "content": pictures})
            pictures = []
        content = message.get("content")
        if message["role"] == "tool" and isinstance(content, list):
            images = [part for part in content if part.get("type") == "image_url"]
            if images:
                marker = {
                    "type": "text",
                    "text": "Untrusted tool output images; tool_call_id="
                    + message["tool_call_id"],
                }
                pictures.extend([marker, *images])
                message = {
                    **message,
                    "content": [
                        part for part in content if part.get("type") != "image_url"
                    ]
                    or [marker],
                }
        output.append(message)
    if pictures:
        output.append({"role": "user", "content": pictures})
    return output


class OpenAICompatibleChatModel(BaseChatModel):
    client: Any = Field(exclude=True, repr=False)
    provider_id: str
    model_name: str
    output_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    reasoning_field: Literal["reasoning_content"] | None = None
    # Explicit vendor capability: ToolStrategy still validates the result schema.
    required_tool_choice: Literal["required", "auto"] = "required"
    extra_body: dict[str, Any] = Field(default_factory=dict)
    temperature: float | None = None
    context_chars_per_token: float = Field(default=2.0, ge=1, le=4)
    disable_streaming: bool = True

    @model_validator(mode="after")
    def validate_client(self):
        if self.client.max_retries != 0:
            raise ValueError("SDK implicit retries must be disabled")
        reserved = {
            "model",
            "messages",
            "tools",
            "tool_choice",
            "stream",
            "n",
            "max_tokens",
            "max_completion_tokens",
            "max_output_tokens",
            "response_format",
        }
        if self.extra_body.keys() & reserved:
            raise ValueError(
                "Provider extensions cannot override the metered request contract"
            )
        return self

    @property
    def _llm_type(self):
        return "research-copilot-openai-compatible"

    def _get_ls_params(self, stop=None, **kwargs):
        return {
            "ls_provider": self.provider_id,
            "ls_model_name": self.model_name,
            "ls_model_type": "chat",
        }

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        settings = {"tools": [convert_to_openai_tool(tool) for tool in tools], **kwargs}
        if tool_choice is not None:
            settings["tool_choice"] = tool_choice
        return self.bind(**settings)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError("Use the asynchronous runtime")

    def _wire_messages(self, messages):
        wire = convert_to_openai_messages(messages)
        if self.reasoning_field:
            for source, target in zip(messages, wire, strict=True):
                if (
                    isinstance(source, AIMessage)
                    and self.reasoning_field in source.additional_kwargs
                ):
                    target[self.reasoning_field] = source.additional_kwargs[
                        self.reasoning_field
                    ]
        return _tool_images_as_user_content(wire)

    def global_input_bound(self, messages, tools):
        # Keep a conservative UTF-8 allowance at the report's spending ceiling.
        # Stage scheduling continues to use the calibrated token estimate.
        wire = self._wire_messages(messages)
        payload = {
            "messages": wire,
            "extra_body": self.extra_body,
            "tools": [convert_to_openai_tool(tool) for tool in (tools or [])],
        }
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 32 * (
            len(wire) + 1
        )

    def prepare_request(self, messages, stop=None, **kwargs):
        """Validate and convert locally before the metering boundary marks sent."""
        settings = {
            "model": self.model_name,
            "messages": self._wire_messages(messages),
            "stream": False,
        }
        if stop is not None:
            settings["stop"] = stop
        if self.temperature is not None:
            settings["temperature"] = self.temperature
        # The metering wrapper uses the standard max_tokens name; map per vendor.
        output_limit = kwargs.pop("max_tokens", None)
        if output_limit is not None:
            settings[self.output_parameter] = output_limit
        allowed = {"tools", "tool_choice", "parallel_tool_calls", "response_format"}
        if set(kwargs) - allowed:
            raise ValueError(
                f"Unsupported model settings: {sorted(set(kwargs) - allowed)}"
            )
        settings.update(kwargs)
        if settings.get("tools"):
            settings["tools"] = [
                convert_to_openai_tool(tool) for tool in settings["tools"]
            ]
        # LangChain ToolStrategy requests `any`; Chat Completions spells the same
        # requirement `required`. Keep the provider wire protocol valid.
        # https://platform.openai.com/docs/api-reference/chat/create
        choice = settings.get("tool_choice")
        if isinstance(choice, str) and choice in {"any", "required"}:
            settings["tool_choice"] = self.required_tool_choice
        elif isinstance(choice, dict) and self.required_tool_choice == "auto":
            raise ValueError("Configured provider does not support forced named tools")
        if self.extra_body:
            settings["extra_body"] = self.extra_body
        return settings

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        settings = self.prepare_request(messages, stop=stop, **kwargs)
        response = await self.client.chat.completions.create(**settings)
        if len(response.choices) != 1:
            raise ValueError("Expected one Chat Completions choice")
        choice = response.choices[0]
        raw = choice.message
        calls, invalid_calls = [], []
        for call in raw.tool_calls or []:
            if call.type != "function":
                raise ValueError("Unsupported tool call type")
            try:
                args = json.loads(call.function.arguments)
                if not isinstance(args, dict):
                    raise ValueError("Tool arguments must be an object")
                calls.append({"id": call.id, "name": call.function.name, "args": args})
            except (ValueError, TypeError) as exc:
                invalid_calls.append(
                    {
                        "id": call.id,
                        "name": call.function.name,
                        "args": call.function.arguments,
                        "error": str(exc),
                    }
                )
        extras = {}
        if (
            self.reasoning_field
            and (reasoning := getattr(raw, self.reasoning_field, None)) is not None
        ):
            extras[self.reasoning_field] = reasoning
        usage = response.usage
        usage_metadata = None
        if usage is not None:
            usage_metadata = {
                "input_tokens": usage.prompt_tokens,
                "output_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            }
            if (
                usage.prompt_tokens_details
                and usage.prompt_tokens_details.cached_tokens is not None
            ):
                usage_metadata["input_token_details"] = {
                    "cache_read": usage.prompt_tokens_details.cached_tokens
                }
            if (
                usage.completion_tokens_details
                and usage.completion_tokens_details.reasoning_tokens is not None
            ):
                usage_metadata["output_token_details"] = {
                    "reasoning": usage.completion_tokens_details.reasoning_tokens
                }
        message = AIMessage(
            content=raw.content or "",
            id=response.id,
            tool_calls=calls,
            invalid_tool_calls=invalid_calls,
            additional_kwargs=extras,
            usage_metadata=usage_metadata,
            response_metadata={
                "provider_id": self.provider_id,
                "model_name": response.model,
                "request_id": getattr(response, "_request_id", None),
                "finish_reason": choice.finish_reason,
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
