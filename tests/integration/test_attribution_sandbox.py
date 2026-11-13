"""Real fixed-runner execution of a full-precision quarter comparison."""
import pytest

from research_copilot.runtime.control import active_dispatch
from research_copilot.subagents.analysist import AnalysisService
from research_copilot.subagents.contracts import MetricSpec
from tests.integration.test_searcher import dispatch
from tests.integration.test_sandbox import service
from tests.unit.test_attribution_precision import comparison, quarter_chain
from tests.unit.test_orchestration import context

pytestmark = pytest.mark.docker


async def test_quarter_comparison_registers_in_one_real_execution(role_env):
    services, _, store = role_env
    inputs, metrics = quarter_chain()
    source_context = {"entity": "CATL", "unit": "元", "currency": "CNY",
                      "period": "comparative", "basis": "consolidated", "scale": "1"}
    raw = {"context": source_context, **{k: v["raw_value"] for k, v in inputs.items()}}
    search = await dispatch(role_env)
    token = active_dispatch.set(search)
    try:
        async def fetch(arguments):
            return raw
        receipt = await services.acquire(context(), "catl", "fixture", "filing", {}, fetch)
    finally:
        active_dispatch.reset(token)
    scope = await dispatch(role_env, "analysist", "catl-declarative")

    def facts(state):
        state.role_data.setdefault("fact_sheets", {})["quarter-facts"] = {
            "facts": [{"input_ref": {"key": k, "raw_value": v["raw_value"], "pointer": "/" + k,
                                     "evidence_id": receipt["evidence_id"], "content_hash": receipt["content_hash"],
                                     **source_context}} for k, v in inputs.items()],
            "evidence_ids": [receipt["evidence_id"]], "plan": None, "dispatch_id": scope.dispatch_id,
            "missing_required": [], "missing_optional": [], "gaps": []}
        state.role_data["scopes"][scope.dispatch_id]["fact_sheet_ids"] = ["quarter-facts"]

    await store.mutate_control(context(), facts)
    metrics[1]["comparison"] = comparison(base_period="2026Q3")
    metrics[3]["comparison"] = comparison(current_period="2026Q2")
    metrics[4]["comparison"] = comparison()
    specs = [MetricSpec.model_validate({k: v for k, v in m.items() if k != "value"}) for m in metrics]
    token = active_dispatch.set(scope)
    try:
        response = await AnalysisService(services, execution=service(role_env)).run_metric_specs(context(), "one", specs, [])
    finally:
        active_dispatch.reset(token)
    assert response["status"] == "registered", response
    state = await store.read_control(context())
    assert len(state.role_data["executions"]) == 1
    changed = state.role_data["metrics"][scope.dispatch_id][response["metric_key_to_id"]["change"]]
    assert changed["value"] == "-2.47"
    assert changed["calculated_value"].startswith("-2.46525896795012")
