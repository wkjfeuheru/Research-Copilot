import pytest

from research_copilot.runtime.control import active_dispatch
from research_copilot.subagents.analysist import AnalysisService
from research_copilot.subagents.contracts import AnalysisCandidate
from tests.integration.test_analysist import financial_evidence
from tests.integration.test_searcher import dispatch
from tests.unit.test_orchestration import context


async def test_analysis_cannot_publish_fabricated_numbers_without_metric(role_env):
    await financial_evidence(role_env)
    scope = await dispatch(role_env, "analysist", "analysis-forged")
    candidate = AnalysisCandidate(
        summary="2024年净利率999.00%",
        conclusions=["2024年净利率999.00%"],
        evidence_ids=["E-000001"],
        claims=[
            {
                "claim_id": "c",
                "text": "2024年净利率999.00%",
                "evidence_ids": ["E-000001"],
            }
        ],
    )
    token = active_dispatch.set(scope)
    try:
        with pytest.raises(ValueError, match="numeric"):
            await AnalysisService(role_env[0]).publish_structured(
                context(), candidate, "forged"
            )
    finally:
        active_dispatch.reset(token)
    assert "analysis" not in (await role_env[2].read_control(context())).artifacts


async def test_disclosed_fact_is_frozen_into_analysis_for_writer_verification(role_env):
    from tests.integration.test_handoff import collected, result

    services, _, store = role_env
    collect, receipt = await collected(role_env)
    await services.map_result(context(), collect, result(receipt))
    scope = await dispatch(role_env, "analysist", "analysis-fact")
    text = "2024年收入120 CNY。"
    binding = {
        "key": "revenue_2024",
        "evidence_id": receipt["evidence_id"],
        "period": "2024",
        "unit": "CNY",
        "scale": "1",
    }
    candidate = AnalysisCandidate(
        summary=text,
        conclusions=[text],
        evidence_ids=[receipt["evidence_id"]],
        claims=[
            {
                "claim_id": "revenue",
                "text": text,
                "evidence_ids": [receipt["evidence_id"]],
                "fact_refs": [binding],
            }
        ],
    )
    token = active_dispatch.set(scope)
    try:
        saved = await AnalysisService(services).publish_structured(
            context(), candidate, "fact"
        )
    finally:
        active_dispatch.reset(token)
    state = await store.read_control(context())
    payload = state.role_data["objects"][saved["analysis_id"]]["payload"]
    assert payload["verified_facts"][0]["input_ref"]["key"] == "revenue_2024"
    assert payload["claims"][0]["fact_refs"] == [binding]


async def test_reviewer_independently_rejects_frozen_unbound_numeric_claim(role_env):
    import hashlib
    import re
    from tests.integration.test_reviewer import submitted_draft, reviewer
    from research_copilot.subagents.services import canonical, digest

    await submitted_draft(role_env)
    services, _, store = role_env

    def corrupt(state):
        ref = state.artifacts["report"]
        obj = state.role_data["objects"][ref.artifact_id]
        payload = obj["payload"]
        payload["document"] = payload["document"].replace(
            "\n", "\n净利率999.00%。\n", 1
        )
        first = re.search(r"^\[\^E-[0-9]{6,}\]:", payload["document"], re.M)
        body = payload["document"][: first.start() - 2]
        payload["body_hash"] = hashlib.sha256(body.encode()).hexdigest()
        payload["claims"] = {"body_hash": payload["body_hash"], "claims": []}
        changed = ref.model_copy(update={"content_hash": digest(payload)})
        obj["ref"] = changed.model_dump()
        (
            services.report_root(context()) / obj["path"].removeprefix("/workspace/")
        ).write_text(canonical(payload), encoding="utf-8")
        state.artifacts["report"] = changed

    await store.mutate_control(context(), corrupt)
    service, _, _ = await reviewer(role_env)
    scope = await dispatch(role_env, "reviewer", "numeric-review")
    token = active_dispatch.set(scope)
    try:
        snapshot = await service.prepare_snapshot(context())
        result = await service.precheck(context(), snapshot)
    finally:
        active_dispatch.reset(token)
    assert any(
        item["category"] == "numeric" and item["hard"] for item in result["issues"]
    )


async def test_reviewer_rechecks_calculation_instead_of_trusting_receipt(role_env):
    from tests.integration.test_reviewer import submitted_draft, reviewer

    await submitted_draft(role_env, quantitative=True)

    def stale(state):
        ref = state.artifacts["analysis"]
        analysis = state.role_data["objects"][ref.artifact_id]
        metrics = state.role_data["metrics"][analysis["dispatch_id"]]
        metric = metrics[analysis["payload"]["metric_ids"][0]]
        metric.update(
            formula_id="difference",
            input_keys=["a", "b"],
            inputs=[{"key": "a", "raw_value": "120"}, {"key": "b", "raw_value": "100"}],
        )

    await role_env[2].mutate_control(context(), stale)
    service, _, _ = await reviewer(role_env)
    scope = await dispatch(role_env, "reviewer", "calculation-review")
    token = active_dispatch.set(scope)
    try:
        snapshot = await service.prepare_snapshot(context())
        checked = await service.precheck(context(), snapshot)
    finally:
        active_dispatch.reset(token)
    assert any(
        i["category"] == "metric" and "formula/value" in i["reason"] and i["hard"]
        for i in checked["issues"]
    )
