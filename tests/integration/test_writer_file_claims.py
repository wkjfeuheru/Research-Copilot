"""Long-report files use the same declarative claim contract as structured output."""

import json
import re

import pytest

from research_copilot.runtime.control import active_dispatch
from research_copilot.subagents.writer import WritingService
from tests.integration.test_searcher import dispatch
from tests.integration.test_writer import confirmed_analysis, markdown
from tests.unit.test_orchestration import context


@pytest.mark.asyncio
async def test_file_claims_are_located_by_host_and_revalidated_after_edit(role_env):
    analysis_id = await confirmed_analysis(role_env, quantitative=True)
    services, _, store = role_env
    scope = await dispatch(role_env, "writer", "file-claims")
    body = markdown(services)
    quote = "收入变化0.20倍。"
    headings = re.findall(r"^## (.+)$", body, re.M)
    body = body.replace("[^E-000001]", quote + "[^E-000001]", 1)
    claims = {
        "sections": [
            {
                "heading": headings[0],
                "claims": [
                    {
                        "quote": quote,
                        "metric_ids": ["M-" + "b" * 16],
                        "evidence_ids": ["E-000001", "E-000002"],
                        "conclusion_id": "growth",
                    }
                ],
            }
        ]
    }
    root = services.report_root(context()) / "tasks/file-claims"
    root.mkdir(parents=True, exist_ok=True)
    (root / "draft.md").write_text(body, encoding="utf-8")
    (root / "claims.json").write_text(json.dumps(claims), encoding="utf-8")
    args = dict(
        candidate_path="/workspace/tasks/file-claims/draft.md",
        claims_path="/workspace/tasks/file-claims/claims.json",
        report_type="company_report",
        analysis_ids=[analysis_id],
    )
    token = active_dispatch.set(scope)
    try:
        service = WritingService(services)
        checked = await service.validate(context(), **args)
        assert checked["ok"], checked["errors"]
        claim = checked["claims"]["claims"][0]
        assert body[claim["start"] : claim["end"]] == quote
        receipt = await service.submit(context(), "first", **args)
        assert receipt["draft_version"] == 1
        (root / "draft.md").write_text(body.replace("0.20", "0.21"), encoding="utf-8")
        with pytest.raises(ValueError, match="quote|numeric|span"):
            await service.submit(context(), "edited", **args)
        state = await store.read_control(context())
        assert state.artifacts["report"].version == "1"
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_file_claim_errors_identify_field_and_required_schema(role_env):
    analysis_id = await confirmed_analysis(role_env)
    services, _, _ = role_env
    scope = await dispatch(role_env, "writer", "bad-file-claims")
    root = services.report_root(context()) / "tasks/bad-file-claims"
    root.mkdir(parents=True, exist_ok=True)
    (root / "draft.md").write_text(markdown(services), encoding="utf-8")
    (root / "claims.json").write_text(
        '{"sections": [{"heading": "1. 投资要点", "claims": [{"evidence_ids": []}]}]}',
        encoding="utf-8",
    )
    token = active_dispatch.set(scope)
    try:
        checked = await WritingService(services).validate(
            context(),
            candidate_path="/workspace/tasks/bad-file-claims/draft.md",
            claims_path="/workspace/tasks/bad-file-claims/claims.json",
            report_type="company_report",
            analysis_ids=[analysis_id],
        )
        assert not checked["ok"]
        assert any(
            "quote" in error and "sections" in error for error in checked["errors"]
        )
    finally:
        active_dispatch.reset(token)
