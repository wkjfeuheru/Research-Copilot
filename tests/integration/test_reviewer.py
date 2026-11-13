"""Frozen reviewer graph, branch barrier and parent interrupt recovery integration."""

import json
import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from research_copilot.runtime.control import active_dispatch, RunControl
from research_copilot.llm import MeteredChatModel
from tests.integration.test_searcher import dispatch
from tests.integration.test_writer import confirmed_analysis, markdown
from tests.integration.support import RespondingModel, native_role
from tests.unit.test_orchestration import context, config, call


async def submitted_draft(env, *, quantitative=False):
    from research_copilot.subagents.writer import WritingService

    analysis_id = await confirmed_analysis(env, quantitative=quantitative)
    services, _, _ = env
    scope = await dispatch(env, "writer", "writing-1")
    token = active_dispatch.set(scope)
    try:
        path = services.report_root(context()) / "tasks/writing-1/draft.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown(services), encoding="utf-8")
        return await WritingService(services).submit(
            context(),
            "submit",
            candidate_path="/workspace/tasks/writing-1/draft.md",
            report_type="company_report",
            analysis_ids=[analysis_id],
        )
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_reviewer_validates_real_calculation_receipt_lineage(role_env, tmp_path):
    await submitted_draft(role_env, quantitative=True)
    service, graph, _ = await reviewer(role_env)
    agent = await native_role(
        role_env,
        tmp_path,
        "reviewer",
        None,
        [],
        None,
        reviewer=graph,
        budget=service.models["quality"].budget,
    )
    result = await agent.ainvoke(
        {"messages": [("user", "review")]}, config(), context=context()
    )
    assert not result.get("__interrupt__")

    def corrupt(state):
        metrics = next(iter(state.role_data["metrics"].values()))
        next(iter(metrics.values()))["inputs"][0]["content_hash"] = "0" * 64

    await role_env[2].mutate_control(context(), corrupt)
    scope = await dispatch(role_env, "reviewer", "invalid-metric")
    token = active_dispatch.set(scope)
    try:
        snapshot = await service.prepare_snapshot(context())
        checked = await service.precheck(context(), snapshot)
        assert any(
            issue["category"] == "metric" and issue["hard"]
            for issue in checked["issues"]
        )
    finally:
        active_dispatch.reset(token)


def reviewing_model(score=0.85, issues=None, *, incomplete=False):
    def respond(messages, step):
        payload = json.loads(
            next(m.content for m in messages if isinstance(m, HumanMessage))
        )
        targets = payload["targets"]
        if step < len(targets):
            target = targets[step]
            return call(
                target["tool"], {"target_id": target["target_id"]}, f"read-{step}"
            )
        return call(
            "BranchReview",
            {
                "pass_confidence": score,
                "checks_complete": not incomplete,
                "covered_targets": [t["target_id"] for t in targets],
                "issues": issues or [],
            },
            "review-output",
        )

    return RespondingModel(script=[], respond=respond)


async def reviewer(env, models=None):
    from research_copilot.subagents.reviewer import (
        ReviewService,
        build_reviewer_graph,
    )

    services, business, store = env
    budget = await store.load_budget(context())
    models = models or (reviewing_model(), reviewing_model())
    metered = [
        MeteredChatModel.wrap(
            m, budget=budget, max_input_tokens=100000, max_output_tokens=1000
        )
        for m in models
    ]
    service = ReviewService(
        services,
        quality_model=metered[0],
        compliance_model=metered[1],
        control=RunControl(),
    )
    business.review_authorizer = service.authorize_resume
    return service, build_reviewer_graph(service), models


@pytest.mark.asyncio
async def test_reviewer_threshold_equal_passes_after_both_branches_and_does_not_sign(
    role_env, tmp_path
):
    receipt = await submitted_draft(role_env)
    service, graph, models = await reviewer(role_env)
    agent = await native_role(
        role_env,
        tmp_path,
        "reviewer",
        None,
        [],
        None,
        reviewer=graph,
        budget=service.models["quality"].budget,
    )
    result = await agent.ainvoke(
        {"messages": [("user", "review")]}, config(), context=context()
    )
    assert not result.get("__interrupt__")
    state = await role_env[2].read_control(context())
    assert state.reviewed_report == state.artifacts["report"]
    assert state.reviewed_report.artifact_id == receipt["draft_id"]
    assert "report" not in state.approved
    record = next(iter(state.role_data["reviews"].values()))
    assert record["pass_confidence"] == 0.85
    assert record["checks_complete"]
    assert all(m.position > 1 for m in models)


@pytest.mark.asyncio
async def test_reviewer_low_score_invalid_feedback_reprompts_without_reassessment(
    role_env, tmp_path
):
    await submitted_draft(role_env)
    service, graph, models = await reviewer(
        role_env, (reviewing_model(0.84), reviewing_model(0.9))
    )
    saver = InMemorySaver()
    agent = await native_role(
        role_env,
        tmp_path,
        "reviewer",
        None,
        [],
        None,
        reviewer=graph,
        saver=saver,
        budget=service.models["quality"].budget,
    )
    first = await agent.ainvoke(
        {"messages": [("user", "review")]}, config(), context=context()
    )
    pending = first["__interrupt__"][0]
    positions = [m.position for m in models]
    invalid = await agent.ainvoke(
        Command(resume={pending.id: {"decision": "approve"}}),
        config(),
        context=context(),
    )
    assert invalid["__interrupt__"][0].value["errors"]
    assert positions == [m.position for m in models]
    payload = invalid["__interrupt__"][0]
    value = {
        "decision": "request_changes",
        "comment": "Clarify limitations",
        "review_session_id": payload.value["review_session_id"],
        "snapshot_id": payload.value["snapshot"]["snapshot_id"],
        "issue_dispositions": [],
    }
    await role_env[1].validate_resume(
        context(), {payload.id: payload.value}, {payload.id: value}
    )
    # Reconstruct the parent graph using the same checkpointer, not a child thread.
    agent = await native_role(
        role_env,
        tmp_path,
        "reviewer",
        None,
        [],
        None,
        reviewer=graph,
        saver=saver,
        budget=service.models["quality"].budget,
    )
    done = await agent.ainvoke(
        Command(resume={payload.id: value}), config(), context=context()
    )
    assert not done.get("__interrupt__")
    assert positions == [m.position for m in models]
    state = await role_env[2].read_control(context())
    assert state.reviewed_report is None
    assert (
        next(iter(state.role_data["reviews"].values()))["outcome"]
        == "changes_requested"
    )


@pytest.mark.asyncio
async def test_high_score_with_major_issue_requires_reasoned_disposition(
    role_env, tmp_path
):
    await submitted_draft(role_env)
    issue = {
        "category": "reasoning",
        "severity": "major",
        "target_id": "document:0",
        "location": "Revenue",
        "quote": "Source-supported",
        "basis": "E-000001",
        "reason": "Check scope",
        "suggestion": "Clarify claim",
    }
    service, graph, _ = await reviewer(
        role_env, (reviewing_model(0.99, [issue]), reviewing_model(0.99))
    )
    agent = await native_role(
        role_env,
        tmp_path,
        "reviewer",
        None,
        [],
        None,
        reviewer=graph,
        budget=service.models["quality"].budget,
    )
    first = await agent.ainvoke(
        {"messages": [("user", "review")]}, config(), context=context()
    )
    pending = first["__interrupt__"][0]
    assert pending.value["assessment"]["blocking_issue_ids"]
    submission = {
        "decision": "approve",
        "comment": "Looks fine",
        "review_session_id": pending.value["review_session_id"],
        "snapshot_id": pending.value["snapshot"]["snapshot_id"],
        "issue_dispositions": [],
    }
    done = await agent.ainvoke(
        Command(resume={pending.id: submission}), config(), context=context()
    )
    assert done["__interrupt__"]
    assert (await role_env[2].read_control(context())).reviewed_report is None
    retry = done["__interrupt__"][0]
    issue_id = retry.value["assessment"]["blocking_issue_ids"][0]
    submission["issue_dispositions"] = [
        {
            "issue_id": issue_id,
            "decision": "false_positive",
            "reason": "The source sentence is qualified by the documented limitations.",
        }
    ]
    final = await agent.ainvoke(
        Command(resume={retry.id: submission}), config(), context=context()
    )
    assert not final.get("__interrupt__")
    assert (await role_env[2].read_control(context())).reviewed_report is not None


@pytest.mark.asyncio
async def test_snapshot_tamper_is_hard_block_and_branch_failure_never_passes(
    role_env, tmp_path
):
    await submitted_draft(role_env)
    service, graph, _ = await reviewer(role_env)
    scope = await dispatch(role_env, "reviewer", "review-1")
    token = active_dispatch.set(scope)
    try:
        snapshot = await service.prepare_snapshot(context())
        content = await service.load_snapshot(context(), snapshot)
        assert any(
            target["text"].startswith("## 2. 公司概况与业务结构")
            for target in content["targets"].values()
        )
        path = service.services.report_root(context()) / snapshot[
            "manifest_path"
        ].removeprefix("/workspace/")
        path.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match="hash"):
            await service.load_snapshot(context(), snapshot)
    finally:
        active_dispatch.reset(token)


def test_text_review_fits_one_model_request_without_omitting_or_marking_targets():
    from research_copilot.subagents.reviewer import full_text_delivery

    content = {
        "quality_targets": ["document:0", "evidence:0"],
        "targets": {
            "document:0": {"text": "结论", "kind": "document"},
            "evidence:0": {"text": "原件数据" * 15000, "kind": "evidence"},
        },
    }
    material = full_text_delivery(
        content, "quality", max_chars=80000, token_headroom=80000
    )
    assert material == {k: v["text"] for k, v in content["targets"].items()}
    assert (
        full_text_delivery(content, "quality", max_chars=900, token_headroom=80000)
        is None
    )
    assert (
        full_text_delivery(content, "quality", max_chars=80000, token_headroom=300)
        is None
    )
    content["targets"]["evidence:0"]["image_base64"] = "AA=="
    assert (
        full_text_delivery(content, "quality", max_chars=80000, token_headroom=80000)
        is None
    )
