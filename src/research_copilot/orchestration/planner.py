"""Planner 的最小 LangGraph 集成。

图结构：``START -> planner -> END``。节点从共享 ``ResearchState`` 读取请求，
校验为 ``PlannerInput``，调用 Planner，校验完成后只返回 ``plan`` 通道的部分更新。

参考：
- LangGraph Graph API / StateGraph：
  https://docs.langchain.com/oss/python/langgraph/use-graph-api
- 节点返回部分状态更新：
  https://docs.langchain.com/oss/python/langgraph/use-graph-api#update-state
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from research_copilot.agents.planner.agent import Planner
from research_copilot.agents.planner.contracts import PlannerInput, PlannerOutput
from research_copilot.state import ResearchState


def build_planner_graph(planner: Planner) -> CompiledStateGraph:
    """构图并编译 ``START -> planner -> END`` 单节点图。"""

    def planner_node(state: ResearchState) -> dict[str, Any]:
        raw_request = state.get("request")
        if raw_request is None:
            raise ValueError("ResearchState 缺少 request，无法调用 Planner")

        request = PlannerInput.model_validate(raw_request)
        output = planner.plan(request)
        return {"plan": output.model_dump(mode="json")}

    builder = StateGraph(ResearchState)
    builder.add_node("planner", planner_node)
    builder.add_edge(START, "planner")
    builder.add_edge("planner", END)
    return builder.compile()


def run_planner_graph(
    graph: CompiledStateGraph,
    request: PlannerInput | Mapping[str, Any],
) -> PlannerOutput:
    """执行 Planner 图并返回校验后的 PlannerOutput。

    接受已校验的 ``PlannerInput`` 或原始 JSON 映射；映射会在节点入口再次经过
    ``PlannerInput.model_validate``，确保图输入也遵守契约。
    """

    raw_request = (
        request.model_dump(mode="json")
        if isinstance(request, PlannerInput)
        else dict(request)
    )
    final_state = graph.invoke({"request": raw_request})
    plan = final_state.get("plan")
    if plan is None:
        raise RuntimeError("Planner 图执行结束但共享状态中没有 plan")

    return PlannerOutput.model_validate(plan)


__all__ = ["build_planner_graph", "run_planner_graph"]
