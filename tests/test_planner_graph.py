"""Planner 单节点 LangGraph 集成测试。"""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from research_copilot.agents.planner.agent import Planner
from research_copilot.agents.planner.contracts import PlannerOutput, PlanningStatus
from research_copilot.agents.planner.errors import PlannerErrorCode, PlannerOutputError
from research_copilot.orchestration.planner import build_planner_graph, run_planner_graph
from research_copilot.state import ResearchState
from tests.planner_fakes import FakeStructuredModelClient
from tests.planner_support import (
    needs_clarification_payload,
    planner_request,
    ready_plan_payload,
)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _graph_with(content: str | Exception):
    client = FakeStructuredModelClient([content])
    graph = build_planner_graph(Planner(client))
    return graph, client


def test_research_state_only_exposes_implemented_channels() -> None:
    assert set(ResearchState.__annotations__) == {"request", "plan"}


def test_single_node_graph_matches_direct_planner_result() -> None:
    request = planner_request()
    payload = _json(ready_plan_payload())

    direct_client = FakeStructuredModelClient([payload])
    graph_client = FakeStructuredModelClient([payload])

    direct = Planner(direct_client).plan(request)
    graph = build_planner_graph(Planner(graph_client))
    from_graph = run_planner_graph(graph, request)

    assert from_graph == direct
    assert from_graph.status is PlanningStatus.READY
    assert len(direct_client.requests) == 1
    assert len(graph_client.requests) == 1
    assert direct_client.requests[0] == graph_client.requests[0]


def test_graph_saves_request_and_json_ready_plan_in_shared_state() -> None:
    request = planner_request()
    graph, client = _graph_with(_json(ready_plan_payload()))
    request_payload = request.model_dump(mode="json")

    final_state = graph.invoke({"request": request_payload})

    assert final_state["request"] == request_payload
    assert isinstance(final_state["plan"], dict)
    assert final_state["plan"]["status"] == "ready"
    assert json.dumps(final_state, ensure_ascii=False)
    assert len(client.requests) == 1

    parsed = PlannerOutput.model_validate(final_state["plan"])
    assert parsed.status is PlanningStatus.READY


def test_graph_accepts_raw_mapping_and_validates_at_node_boundary() -> None:
    request = planner_request()
    graph, _ = _graph_with(_json(ready_plan_payload()))

    output = run_planner_graph(graph, request.model_dump(mode="json"))

    assert output.status is PlanningStatus.READY


def test_graph_returns_needs_clarification_without_branching() -> None:
    request = planner_request()
    graph, _ = _graph_with(_json(needs_clarification_payload()))

    output = run_planner_graph(graph, request)

    assert output.status is PlanningStatus.NEEDS_CLARIFICATION
    assert output.clarification_questions


def test_graph_propagates_planner_output_error() -> None:
    graph, _ = _graph_with("not-json")

    with pytest.raises(PlannerOutputError) as exc_info:
        run_planner_graph(graph, planner_request())

    assert exc_info.value.code is PlannerErrorCode.INVALID_JSON


def test_graph_rejects_invalid_input_mapping_at_node_boundary() -> None:
    graph, client = _graph_with(_json(ready_plan_payload()))

    with pytest.raises(ValidationError):
        run_planner_graph(graph, {"market_scope": "a_share"})

    assert client.requests == []
