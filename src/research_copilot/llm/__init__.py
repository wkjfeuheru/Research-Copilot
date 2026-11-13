"""Budget boundary around a configured LangChain chat-model adapter.

Provider protocol conversion stays with the injected adapter. This boundary also
meters direct summarizer calls, which may bypass ordinary agent middleware.
"""

import json
import hashlib
import math
import time
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from research_copilot.runtime.budget import TokenBudget

# 校准 token 估算权重：现代分词器下中文约 0.6 token/字、其余约 0.25 token/字符。
# 字节法会把中文负载高估约 3 倍，导致阶段预算被预留虚耗而过早阻断。
_CJK_RANGES = (
    ("\u2e80", "\u9fff"),
    ("\u3400", "\u4dbf"),
    ("\uf900", "\ufaff"),
    ("\uff00", "\uffef"),
)


def estimate_payload_tokens(payload_text, *, margin=1.25):
    """按字符类别估算 token 数，加固定安全余量；可离线按 provider 账单再校准。"""
    cjk = sum(
        1 for ch in payload_text if any(low <= ch <= high for low, high in _CJK_RANGES)
    )
    other = len(payload_text) - cjk
    return math.ceil((cjk * 0.6 + other * 0.25) * margin)


class MeteredChatModel(BaseChatModel):
    inner: BaseChatModel = Field(exclude=True, repr=False)
    budget: Any = Field(exclude=True, repr=False)
    model_name: str
    budget_stage: str = "supervisor"
    budget_branch: str | None = None
    max_input_tokens: int
    max_output_tokens: int
    output_parameter: str = "max_tokens"
    disable_streaming: bool = True
    request_audit: Any = Field(default=None, exclude=True, repr=False)
    before_request: Any = Field(default=None, exclude=True, repr=False)
    context_chars_per_token: float = Field(default=2.0, ge=1, le=4)

    @classmethod
    def wrap(
        cls,
        model,
        *,
        budget: TokenBudget,
        max_input_tokens: int,
        max_output_tokens: int,
        output_parameter="max_tokens",
        request_audit=None,
        before_request=None,
        budget_stage="supervisor",
        budget_branch=None,
    ):
        if not 0 < max_output_tokens < max_input_tokens:
            raise ValueError("Explicit context and output capacities are required")
        if output_parameter not in {
            "max_tokens",
            "max_completion_tokens",
            "max_output_tokens",
        }:
            raise ValueError("Unsupported output token parameter")
        return cls(
            inner=model,
            budget=budget,
            budget_stage=budget_stage,
            budget_branch=budget_branch,
            model_name=getattr(model, "model_name", None)
            or getattr(model, "model", "")
            or "",
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            output_parameter=output_parameter,
            request_audit=request_audit,
            before_request=before_request,
            context_chars_per_token=getattr(model, "context_chars_per_token", 2.0),
            profile={
                **(model.profile or {}),
                "max_input_tokens": max_input_tokens,
                "max_output_tokens": max_output_tokens,
                "tool_calling": True,
            },
        )

    @property
    def _llm_type(self):
        return "research-copilot-metered"

    def _get_ls_params(self, stop=None, **kwargs):
        return self.inner._get_ls_params(stop=stop, **kwargs)

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        settings = {"tools": list(tools), **kwargs}
        if tool_choice is not None:
            settings["tool_choice"] = tool_choice
        return self.bind(**settings)

    def get_num_tokens_from_messages(self, messages, tools=None):
        # Scheduling estimate, not a billing or capacity guarantee. The explicit
        # per-model ratio can be calibrated offline against provider usage.
        from langchain_core.messages import convert_to_openai_messages

        payload = {
            "messages": convert_to_openai_messages(messages),
            "tools": [convert_to_openai_tool(tool) for tool in (tools or [])],
        }
        # The adapter may send reasoning content; include it in the estimate.
        reasoning = sum(
            len(str(m.additional_kwargs.get("reasoning_content", ""))) for m in messages
        )
        return math.ceil(
            (len(json.dumps(payload, ensure_ascii=False)) + reasoning)
            / self.context_chars_per_token
        ) + 32 * len(messages)

    def reservation_input_tokens(self, messages, tools=None):
        # 统一校准估算：字符类别加权 + 25% 余量，不再用 UTF-8 字节数冒充 token。
        payload = {
            "messages": [message.model_dump(mode="json") for message in messages],
            "tools": [convert_to_openai_tool(tool) for tool in (tools or [])],
        }
        return estimate_payload_tokens(
            json.dumps(payload, ensure_ascii=False)
        ) + 32 * len(messages)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError(
            "model requests require the asynchronous session runtime"
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.before_request:
            await self.before_request()
        tools = kwargs.pop("tools", None)
        estimated_input = self.reservation_input_tokens(messages, tools)
        context_estimate = self.get_num_tokens_from_messages(messages, tools)
        if context_estimate + self.max_output_tokens > self.max_input_tokens:
            # 用 ContextOverflowError 而非 ValueError：Deep Agents 的摘要中间件
            # 会识别该类型并压缩历史后重试，否则整轮运行直接失败。
            from langchain_core.exceptions import ContextOverflowError

            raise ContextOverflowError("Request exceeds configured context capacity")
        if any(
            key in kwargs
            for key in {"max_tokens", "max_completion_tokens", "max_output_tokens"}
        ):
            raise ValueError("Output token capacity cannot be overridden at runtime")
        from research_copilot.runtime.control import active_dispatch

        scope = active_dispatch.get()
        stage = (
            (scope.role if scope else "supervisor")
            if self.budget_stage == "summarizer"
            else self.budget_stage
        )
        global_bound = getattr(self.inner, "global_input_bound", None)
        metadata = getattr(run_manager, "metadata", None) or {}
        node_identity = metadata.get("langgraph_checkpoint_ns")
        # 中间件的内部调用（摘要、工具选择等）与主调用共享同一 checkpoint 命名空间。
        # operation_id 必须区分调用通道，否则预算的防重放守卫会把同一节点里
        # 紧随摘要调用的主调用误判为重放并以 ValueError 拒绝，击穿整个节点。
        call_channel = metadata.get("lc_source") or (
            "internal" if metadata.get("lc_internal_call") else "main"
        )
        operation_id = None
        if node_identity:
            operation_id = hashlib.sha256(
                json.dumps(
                    [
                        self.budget.budget_id,
                        node_identity,
                        self.model_name,
                        self.budget_stage,
                        self.budget_branch,
                        scope.dispatch_id if scope else None,
                        call_channel,
                    ],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
        from research_copilot.runtime.budget import BudgetExceeded

        try:
            reservation = await self.budget.reserve(
                estimated_input + self.max_output_tokens,
                global_bound=global_bound(messages, tools) + self.max_output_tokens
                if global_bound
                else None,
                stage=stage,
                branch=self.budget_branch,
                operation_id=operation_id,
            )
        except (BudgetExceeded, ValueError) as exc:
            if self.request_audit:
                await self.request_audit(
                    {
                        "event": "admission_rejected",
                        "model": self.model_name,
                        "stage": stage,
                        "error_type": type(exc).__name__,
                        "operation_id": operation_id,
                    }
                )
            raise
        usage = None
        details = {}
        started = time.monotonic()
        sent = False
        try:
            if self.request_audit:
                await self.request_audit(
                    {
                        "event": "started",
                        "request_id": reservation,
                        "model": self.model_name,
                        "estimated_input_tokens": estimated_input,
                        "context_estimate_tokens": context_estimate,
                        "max_output_tokens": self.max_output_tokens,
                    }
                )
            target = self.inner.bind_tools(tools) if tools else self.inner
            prepare = getattr(self.inner, "prepare_request", None)
            # Our SDK adapter alone owns vendor parameter translation.
            output_parameter = "max_tokens" if prepare else self.output_parameter
            settings = {output_parameter: self.max_output_tokens, **kwargs}
            if stop is not None:
                settings["stop"] = stop
            if prepare:
                prepare(messages, **settings, **({"tools": tools} if tools else {}))
            if self.before_request:
                await self.before_request()
            sent = True
            await self.budget.mark_sent(reservation)
            response = await target.ainvoke(messages, **settings)
            if response.usage_metadata:
                metadata = response.usage_metadata
                counts = [metadata.get("input_tokens"), metadata.get("output_tokens")]
                if all(type(v) is int and v >= 0 for v in counts):
                    usage = sum(counts)
                details = {
                    "input_tokens": metadata["input_tokens"],
                    "output_tokens": metadata["output_tokens"],
                    "cache_read_tokens": metadata.get("input_token_details", {}).get(
                        "cache_read", 0
                    ),
                    "reasoning_tokens": metadata.get("output_token_details", {}).get(
                        "reasoning", 0
                    ),
                }
                if usage is not None and metadata.get("total_tokens") != usage:
                    # total 常因 reasoning/cache 计费差值与分项不一致：采用可信的
                    # 分项和作为暂定用量并标记待对账，避免整单退化为按预留值
                    # 扣款的未知占额。
                    details["usage_provisional"] = True
            details["finish_reason"] = response.response_metadata.get("finish_reason")
            details["provider_request_id"] = response.response_metadata.get(
                "request_id"
            )
            details["provider_response_id"] = response.id
            return ChatResult(generations=[ChatGeneration(message=response)])
        except BaseException as exc:
            details["error_type"] = type(exc).__name__
            raise
        finally:
            # Cancellation and transport errors retain an unknown reservation charge.
            if sent:
                await self.budget.settle(
                    reservation, usage, details={"model": self.model_name, **details}
                )
            else:
                await self.budget.release_unsent(reservation)
            if self.request_audit:
                await self.request_audit(
                    {
                        "event": "settled",
                        "request_id": reservation,
                        "model": self.model_name,
                        "usage_tokens": usage,
                        "usage_known": usage is not None,
                        "sent": sent,
                        "elapsed_seconds": time.monotonic() - started,
                        **details,
                    }
                )
