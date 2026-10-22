"""Planner 契约与 JSON Schema 导出测试。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from research_copilot.agents.planner.contracts import (
    Capability,
    MarketScope,
    MaterialType,
    PlannerInput,
    PlannerOutput,
    PlanningStatus,
    ResearchObjectType,
    export_planner_schemas,
    planner_input_schema,
    planner_output_schema,
)
from research_copilot.llm.client import ModelCallError, ModelRequest, ModelResponse
from tests.planner_support import ready_plan_payload


def test_minimal_input_has_a_share_scope_and_no_default_date() -> None:
    request = PlannerInput(research_question="  贵州茅台 2025 年利润增长质量如何？  ")

    assert request.research_question == "贵州茅台 2025 年利润增长质量如何？"
    assert request.market_scope is MarketScope.A_SHARE
    assert request.cutoff_date is None
    assert request.selected_skill_ids == []
    assert request.attachments == []
    assert request.user_notes is None


def test_input_rejects_unknown_fields_and_attachment_content() -> None:
    with pytest.raises(ValidationError):
        PlannerInput(research_question="研究问题", unexpected="not allowed")

    with pytest.raises(ValidationError):
        PlannerInput(
            research_question="研究问题",
            attachments=[
                {
                    "attachment_id": "attachment-1",
                    "filename": "report.pdf",
                    "content": "文件正文不应进入输入契约",
                }
            ],
        )


def test_input_rejects_duplicate_skills_and_attachments() -> None:
    with pytest.raises(ValidationError):
        PlannerInput(
            research_question="研究问题",
            selected_skill_ids=["financial-analysis", "financial-analysis"],
        )

    with pytest.raises(ValidationError):
        PlannerInput(
            research_question="研究问题",
            attachments=[
                {"attachment_id": "a1", "filename": "one.pdf"},
                {"attachment_id": "a1", "filename": "two.pdf"},
            ],
        )


def test_input_cutoff_date_serializes_for_graph_transport() -> None:
    request = PlannerInput(
        research_question="行业研究",
        cutoff_date="2025-12-31",
    )

    assert request.model_dump(mode="json")["cutoff_date"] == "2025-12-31"


def test_output_accepts_ready_plan_payload() -> None:
    output = PlannerOutput.model_validate(ready_plan_payload())

    assert output.status is PlanningStatus.READY
    assert output.research_scope.object_type is ResearchObjectType.STOCK
    assert output.objective is not None
    assert output.steps[0].capabilities == [Capability.EVIDENCE_ACQUISITION]
    assert output.data_requirements[0].material_types == [
        MaterialType.FINANCIAL_STATEMENT
    ]
    assert output.model_dump(mode="json")["status"] == "ready"


def test_output_accepts_unsupported_without_objective() -> None:
    output = PlannerOutput.model_validate(
        {
            "status": "unsupported",
            "research_scope": {
                "object_type": "stock",
                "subject": "海外市场标的",
            },
            "unsupported_reason": "当前只支持 A 股个股、行业和公司对比。",
        }
    )

    assert output.status is PlanningStatus.UNSUPPORTED
    assert output.objective is None


def test_schema_export_writes_json_files(tmp_path: Path) -> None:
    input_path, output_path = export_planner_schemas(tmp_path)

    input_schema = json.loads(input_path.read_text(encoding="utf-8"))
    output_schema = json.loads(output_path.read_text(encoding="utf-8"))

    assert input_schema == planner_input_schema()
    assert output_schema == planner_output_schema()
    assert input_schema["title"] == "PlannerInput"
    assert "research_question" in input_schema["properties"]
    assert output_schema["title"] == "PlannerOutput"
    assert "status" in output_schema["properties"]


def test_committed_schemas_are_current() -> None:
    schema_dir = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "research_copilot"
        / "agents"
        / "planner"
        / "schemas"
    )

    assert json.loads(
        (schema_dir / "planner_input.schema.json").read_text(encoding="utf-8")
    ) == planner_input_schema()
    assert json.loads(
        (schema_dir / "planner_output.schema.json").read_text(encoding="utf-8")
    ) == planner_output_schema()


def test_model_boundary_returns_raw_text() -> None:
    request = ModelRequest(
        system_prompt="system",
        user_prompt="user",
        response_schema=planner_output_schema(),
        prompt_version="test-v1",
    )
    response = ModelResponse(content='{"status": "ready"}', model="fake")

    assert request.response_schema["title"] == "PlannerOutput"
    assert response.content == '{"status": "ready"}'
    assert response.model == "fake"
    assert issubclass(ModelCallError, RuntimeError)
