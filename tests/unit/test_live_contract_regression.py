"""Regression for the contracts encountered during the Wuliangye live run."""

import json
from types import SimpleNamespace

import pytest

from research_copilot.contracts import Dispatch, TaskBrief
from research_copilot.runtime.control import RunControl
from research_copilot.runtime.budget import BudgetExceeded
from research_copilot.sandbox.execution_policy import check_execution_policy
from research_copilot.subagents.contracts import BusinessInputError
from research_copilot.subagents.middleware import RoleBusinessMiddleware
from research_copilot.subagents.services import digest
from tests.unit.test_autonomous_dispatch import setup, admit, brief
from tests.unit.test_orchestration import context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields,optional,cap",
    [
        (["营业收入2024"], [], 15),
        (["revenue"] * 2, [], 15),
        (["revenue"], ["revenue"], 15),
        ([f"field_{i}" for i in range(28)], [], 15),
    ],
)
async def test_impossible_fact_contract_is_rejected_before_registration(
    tmp_path, fields, optional, cap
):
    repo, _, services = await setup(tmp_path)
    task = brief("bad", required_fields=fields, optional_fields=optional, max_facts=cap)
    with pytest.raises(ValueError, match="field|facts"):
        await admit(services, "bad", task)
    assert not repo.state.dispatches
    assert not repo.state.role_data.get("tasks")


@pytest.mark.asyncio
async def test_analysis_requires_validated_fact_inputs_before_admission(tmp_path):
    repo, _, services = await setup(tmp_path)
    task = TaskBrief(
        task_id="analysis",
        role="analysist",
        objective="growth",
        as_of="2025-04-30",
        required_fields=["revenue_2024", "revenue_2023"],
        required_metrics=[{"key": "growth", "semantic_type": "growth", "unit": "%"}],
    )
    with pytest.raises(ValueError, match="fact"):
        await services.prepare_dispatch(
            context(),
            Dispatch("a", "analysist", "/workspace/tasks/a/"),
            task.model_dump_json(),
        )
    assert not repo.state.dispatches


@pytest.mark.asyncio
async def test_declarative_analysis_requires_explicit_metrics(tmp_path):
    repo, _, services = await setup(tmp_path)
    task = TaskBrief(
        task_id="analysis", role="analysist", objective="growth", as_of="2025-04-30"
    )
    with pytest.raises(ValueError, match="required_metrics"):
        await services.prepare_dispatch(
            context(),
            Dispatch("a", "analysist", "/workspace/tasks/a/"),
            task.model_dump_json(),
        )
    assert not repo.state.dispatches


@pytest.mark.asyncio
async def test_standard_metric_cannot_launch_freeform_code(tmp_path):
    script = tmp_path / "tasks/d/run.py"
    script.parent.mkdir(parents=True)
    script.write_text("print(1)")
    state = SimpleNamespace(
        role_data={
            "workflow_version": "products-v2",
            "scopes": {"d": {"task_brief": {"calculation_mode": "declarative"}}},
        }
    )

    class Services:
        async def scope(self, context, role):
            return SimpleNamespace(
                dispatch_id="d", write_prefix="/workspace/tasks/d/"
            ), state

        def report_root(self, context):
            return tmp_path

    async def forbidden(request):
        pytest.fail("standard calculation must not reach the sandbox backend")

    request = SimpleNamespace(
        state={},
        runtime=SimpleNamespace(context=None),
        tool_call={
            "id": "call",
            "name": "execute",
            "args": {"command": "python /workspace/tasks/d/run.py"},
        },
    )
    response = await RoleBusinessMiddleware(
        "analysist", Services(), RunControl()
    ).awrap_tool_call(request, forbidden)
    error = json.loads(response.content)["error"]
    assert error["code"] == "declarative_calculation_required"
    assert error["attempt_consumed"] is False


def test_execution_cap_exposes_precise_quota_details():
    scopes = {"d": {"task_brief": {"max_executions": 2}}}
    records = [
        {
            "dispatch_id": "d",
            "status": "failed",
            "execution_id": str(i),
            "output": "failed",
        }
        for i in range(2)
    ]
    with pytest.raises(BudgetExceeded) as raised:
        check_execution_policy("d", scopes, {"d": {}}, records, 20)
    assert raised.value.details == {
        "code": "sandbox_execution_limit",
        "used": 2,
        "limit": 2,
    }


@pytest.mark.asyncio
async def test_declarative_model_sees_only_supported_execution_entry(tmp_path):
    from tests.unit.test_collection_convergence import environment

    mw, request, state = environment(
        {"task_brief": {"calculation_mode": "declarative"}, "evidence_ids": []}
    )
    mw.role = "analysist"
    mw.services.report_root = lambda context: tmp_path
    request.tools = [
        {"name": "execute"},
        {"name": "run_metric_specs"},
        {"name": "read_file"},
    ]

    async def capture(updated):
        assert {t["name"] for t in updated.tools} == {"run_metric_specs", "read_file"}
        assert (
            "then write code and inputs, execute once"
            not in updated.system_message.content
        )

    await mw.awrap_model_call(request, capture)


@pytest.mark.asyncio
async def test_truncated_collection_needs_explicit_single_repair(tmp_path):
    from research_copilot.runtime.autonomy import stop_reason

    repo, _, services = await setup(tmp_path)
    await admit(services, "original")

    def truncated(task_id):
        return {
            "task_id": task_id,
            "status": "partial",
            "stop_reason": "blocked",
            "summary": "truncated",
            "source_warnings": ["truncated_search_result"],
            "gaps": [
                {
                    "description": "missing",
                    "impact": "no facts",
                    "suggested_role": "supervisor",
                }
            ],
        }

    await services.map_result(
        context(),
        Dispatch("original", "searcher", "/workspace/tasks/original/"),
        truncated("original"),
    )
    with pytest.raises(ValueError, match="repair"):
        await admit(services, "renamed")
    await admit(services, "repair", brief("repair", repair_of="original"))
    await services.map_result(
        context(),
        Dispatch("repair", "searcher", "/workspace/tasks/repair/"),
        truncated("repair"),
    )
    assert "manual recovery" in stop_reason(repo.state)


def test_historical_incompatible_task_still_deserializes():
    old = brief("legacy", required_fields=["营业收入"], max_facts=1).model_dump()
    old.pop("calculation_mode")
    assert TaskBrief.model_validate(old).required_fields == ["营业收入"]


def test_autonomous_supervisor_prompt_covers_chart_and_qualitative_rules():
    from research_copilot.orchestration import AUTONOMOUS_SUPERVISOR_PROMPT

    # 必要范围澄清使用持久化提问；图表偏好采用默认值，不额外阻断研究。
    assert "ask_user" in AUTONOMOUS_SUPERVISOR_PROMPT
    assert "不为图表偏好单独暂停或提问" in AUTONOMOUS_SUPERVISOR_PROMPT
    assert "尊重用户已说明的要求" in AUTONOMOUS_SUPERVISOR_PROMPT
    # 需要图表时分析派单用 custom 模式
    assert "calculation_mode=custom" in AUTONOMOUS_SUPERVISOR_PROMPT
    assert "run_metric_specs" in AUTONOMOUS_SUPERVISOR_PROMPT
    # 图表任务需多次沙盒执行，execution_purposes 必须逐项声明（WLY 验收缺陷）
    assert "execution_purposes" in AUTONOMOUS_SUPERVISOR_PROMPT
    assert "逐项声明" in AUTONOMOUS_SUPERVISOR_PROMPT
    assert "register_metrics" in AUTONOMOUS_SUPERVISOR_PROMPT
    assert "render_chart" in AUTONOMOUS_SUPERVISOR_PROMPT
    # 脚本首跑可能因产物清单格式失败：必须预留修复用途并同步执行额度（第三轮缺陷）
    assert "render_chart_repair" in AUTONOMOUS_SUPERVISOR_PROMPT
    assert "预留一项修复用途" in AUTONOMOUS_SUPERVISOR_PROMPT
    assert "analysis_execution_limit" in AUTONOMOUS_SUPERVISOR_PROMPT
    # 图表传递给 writer
    assert "chart:C-id" in AUTONOMOUS_SUPERVISOR_PROMPT
    # 定性事实必须进入冻结字段集
    assert "定性事实" in AUTONOMOUS_SUPERVISOR_PROMPT
    assert (
        "不得只在 objective 中要求登记冻结字段之外的事实"
        in AUTONOMOUS_SUPERVISOR_PROMPT
    )
    # 派生链所需的全部累计期间必须一次登记（CATL 验收缺陷）
    assert "全部累计期间" in AUTONOMOUS_SUPERVISOR_PROMPT
    assert "上年同期" in AUTONOMOUS_SUPERVISOR_PROMPT


def test_searcher_prompt_requires_full_period_chain_without_derived_inputs():
    from research_copilot.subagents.prompts import SEARCHER_PROMPT

    assert "全部累计期间" in SEARCHER_PROMPT
    assert "上年同期" in SEARCHER_PROMPT
    assert "派生值不是原始输入" in SEARCHER_PROMPT


@pytest.mark.asyncio
async def test_enumerated_qualitative_keys_must_be_frozen(tmp_path):
    repo, _, services = await setup(tmp_path)
    task = brief(
        "qual",
        required_fields=["revenue_2024"],
        optional_fields=["gross_margin_pct_2024"],
    ).model_copy(
        update={
            "objective": (
                "采集年报数据。另请登记不超过4条定性事实，key用稳定英文标识"
                "（如 highlight_product_structure、risk_industry_demand）。"
            )
        }
    )
    with pytest.raises(ValueError, match="highlight_product_structure"):
        await admit(services, "qual", task)
    assert not repo.state.dispatches
    assert not repo.state.role_data.get("tasks")


@pytest.mark.asyncio
async def test_frozen_qualitative_keys_are_admitted(tmp_path):
    repo, _, services = await setup(tmp_path)
    task = brief(
        "qual",
        required_fields=["revenue_2024"],
        optional_fields=["highlight_direct_channel"],
    ).model_copy(
        update={
            "objective": (
                "采集年报数据。另请登记定性事实，key用稳定英文标识"
                "（如 highlight_direct_channel）。"
            )
        }
    )
    await admit(services, "qual", task)
    assert repo.state.dispatches["qual"]["role"] == "searcher"


@pytest.mark.asyncio
async def test_ordinary_terms_do_not_trigger_enumeration_rejection(tmp_path):
    repo, _, services = await setup(tmp_path)
    task = brief(
        "terms",
        required_fields=["revenue_2024", "revenue_2023"],
    ).model_copy(
        update={
            "objective": (
                "使用 flash_comment 模板和 run_metric_specs 工具。"
                "字段含义：revenue_2024/revenue_2023 为合并口径营业收入。"
            )
        }
    )
    await admit(services, "terms", task)
    assert repo.state.dispatches["terms"]["role"] == "searcher"


def test_qualitative_text_fact_is_accepted_when_key_is_frozen():
    from research_copilot.subagents.facts import validate_facts

    raw = {"body": "直营渠道建设成效显著，新增138家专卖店。"}
    fact = SimpleNamespace(
        key="highlight_direct_channel",
        evidence_id="E-1",
        content_hash=digest(raw),
        pointer="/body",
        quote="直营渠道建设成效显著，新增138家专卖店",
        raw_value="直营渠道建设成效显著，新增138家专卖店",
        unit="文字",
        period="2024年度",
        currency="不适用",
        basis="原文披露",
    )
    fact.model_dump = lambda **kwargs: {"key": fact.key}
    result = SimpleNamespace(
        facts=[fact],
        evidence_ids=["E-1"],
        status="completed",
        stop_reason="required_complete",
    )
    state = SimpleNamespace(
        role_data={
            "evidence": {
                "E-1": {
                    "raw": raw,
                    "content_hash": digest(raw),
                    "source": {"server": "tavily"},
                    "collected_at": "2025-04-30",
                    "kind": "original_text",
                }
            }
        }
    )
    facts = validate_facts(
        result,
        state,
        {
            "evidence_ids": ["E-1"],
            "task_brief": {"required_fields": ["highlight_direct_channel"]},
        },
    )
    assert facts[0]["input_ref"]["key"] == "highlight_direct_channel"
    assert facts[0]["verification"] == "source-content-match-only"


def test_frozen_field_rejection_lists_fields_and_repair_hint():
    from research_copilot.subagents.facts import validate_facts

    raw = {"body": "直营渠道建设成效显著，新增138家专卖店。"}
    fact = SimpleNamespace(
        key="highlight_direct_channel",
        evidence_id="E-1",
        content_hash=digest(raw),
        pointer="/body",
        quote="直营渠道建设成效显著，新增138家专卖店",
        raw_value="直营渠道建设成效显著，新增138家专卖店",
        unit="文字",
        period="2024年度",
    )
    fact.model_dump = lambda **kwargs: {}
    result = SimpleNamespace(
        facts=[fact],
        evidence_ids=["E-1"],
        status="completed",
        stop_reason="required_complete",
    )
    state = SimpleNamespace(
        role_data={
            "evidence": {
                "E-1": {
                    "raw": raw,
                    "content_hash": digest(raw),
                    "source": {"server": "tavily"},
                    "collected_at": "2025-04-30",
                    "kind": "original_text",
                }
            }
        }
    )
    with pytest.raises(BusinessInputError) as error:
        validate_facts(
            result,
            state,
            {
                "evidence_ids": ["E-1"],
                "task_brief": {
                    "required_fields": ["revenue_2024"],
                    "optional_fields": ["gross_margin_pct_2024"],
                },
            },
        )
    message = str(error.value)
    assert "revenue_2024" in message and "gross_margin_pct_2024" in message
    assert "optional_fields" in message
