"""Deterministic tool routing over the existing checkpointed execution nodes."""

import json

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from research_copilot.runtime.budget import BudgetExceeded
from research_copilot.runtime.workflow import WorkflowBlocked, decide_next


class ProductWorkflowMiddleware(AgentMiddleware):
    def __init__(self, business, control, budget):
        self.business, self.control, self.budget = business, control, budget

    async def awrap_model_call(self, request, handler):
        await self.control.acheck()
        state = await self.business.repository.read_control(request.runtime.context)
        action = decide_next(state, request.runtime.context.report_id)
        if action["kind"] == "complete":
            return ModelResponse(
                result=[AIMessage(content="Report signed and workflow complete.")]
            )
        if action["kind"] == "plan":
            names = set(action["tools"])
            tools = [
                t
                for t in request.tools
                if (
                    t.name
                    if hasattr(t, "name")
                    else t.get("name", t.get("function", {}).get("name"))
                )
                in names
            ]
            prompt = request.system_message.content if request.system_message else ""
            prompt += (
                "\n采集 required_fields 仅列可逐字引用的披露值；分析 required_fields/required_metrics "
                "列沙盒计算输出，键不得重复。单季比较应采集当年一季度及半年度累计收入和成本，"
                "由分析做累计相减、比率和百分点变动。披露不足的驱动归因列为局限；"
                "可选信息缺失不阻止交付。直接披露并仅引用的指标可作为采集事实。"
            )
            prompt += "\nProduct workflow: return a plan with bounded searcher collection tasks and exactly one analysist, writer, reviewer task. Unique task_id; same as_of. Only propose the plan or rework; host routes approved products and confirmation gates."
            prompt += " If essential research inputs are missing, ask the user a concise clarification in plain text. A clarification is not a submitted or approved plan."
            response = await handler(
                request.override(
                    tools=tools, system_message=SystemMessage(content=prompt)
                )
            )
            if any(
                c["name"] not in names
                for m in response.result
                if isinstance(m, AIMessage)
                for c in m.tool_calls
            ):
                raise WorkflowBlocked(
                    "Planner attempted an operation outside its proposal scope"
                )
            if not any(
                isinstance(m, AIMessage) and m.tool_calls for m in response.result
            ):
                answers = [
                    m for m in response.result if isinstance(m, AIMessage) and m.content
                ]
                if not answers:
                    raise WorkflowBlocked("Planner returned an empty response")
                for answer in answers:
                    answer.additional_kwargs["awaiting_user_input"] = True
            return response
        # A handled rejection/error must not be silently retried with the same identity.
        previous = next(
            (
                m
                for m in reversed(request.messages)
                if isinstance(m, ToolMessage) and m.tool_call_id == action["id"]
            ),
            None,
        )
        if previous is not None:
            try:
                error = json.loads(previous.content)
            except (ValueError, TypeError):
                error = {}
            if (
                isinstance(error, dict)
                and error.get("error") == "stage_budget_exhausted"
            ):
                details = error.get("details")
                raise BudgetExceeded(
                    "Product stage reservation rejected",
                    details=details if isinstance(details, dict) else {},
                )
            if (
                isinstance(error, dict)
                and error.get("error") == "invalid_subagent_result"
            ):
                raise WorkflowBlocked(
                    "子任务未提交有效的结构化结果，可能因输出被截断。请核查已保存的来源并调整任务范围。"
                )
            raise WorkflowBlocked(
                "Action was rejected or did not commit a valid result; explicit correction required"
            )
        if action["name"] == "task":
            b = self.budget.snapshot()
            if (b["call_limit"] is not None and b["calls"] >= b["call_limit"]) or b[
                "charged_tokens"
            ] + sum(b["pending"].values()) >= b["limit"]:
                raise BudgetExceeded("Product dispatch budget exhausted")
            role = action["args"]["subagent_type"]
            cap = b.get("stage_limits", {}).get(role, {})
            used = b.get("stages", {}).get(role, {})
            if cap and used.get("calls", 0) >= cap["calls"]:
                raise BudgetExceeded("Product stage call budget exhausted")
        if action["name"] == "signoff_report":
            from research_copilot.runtime.signoff import signoff_error

            budget = await self.business.repository.load_budget(request.runtime.context)
            if error := signoff_error(state, budget.status()):
                raise WorkflowBlocked(error)
        return ModelResponse(
            result=[
                AIMessage(
                    content="",
                    tool_calls=[{k: action[k] for k in ("name", "args", "id")}],
                )
            ]
        )
