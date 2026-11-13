import json
import pytest
from research_copilot.runtime.control import active_dispatch
from tests.integration.test_searcher import dispatch
from tests.integration.test_writer import confirmed_analysis, markdown
from tests.unit.test_orchestration import context


@pytest.mark.asyncio
async def test_writer_cannot_submit_untracked_numeric_claim(role_env):
    from research_copilot.subagents.writer import WritingService

    analysis_id = await confirmed_analysis(role_env)
    scope = await dispatch(role_env, "writer", "write-q")
    token = active_dispatch.set(scope)
    try:
        path = role_env[0].report_root(context()) / "tasks/write-q/draft.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            markdown(role_env[0]).replace(
                "Source-supported", "Profit rose 99.99% Source-supported", 1
            ),
            encoding="utf-8",
        )
        result = await WritingService(role_env[0]).validate(
            context(),
            candidate_path="/workspace/tasks/write-q/draft.md",
            report_type="company_report",
            analysis_ids=[analysis_id],
        )
        assert not result["ok"]
        assert any("claim" in e for e in result["errors"])
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_report_as_of_cannot_change_across_dispatches(role_env):
    await dispatch(
        role_env,
        description=json.dumps(
            {
                "task_id": "collect",
                "role": "searcher",
                "objective": "facts",
                "as_of": "2025-04-30",
            }
        ),
    )
    with pytest.raises(ValueError, match="as_of"):
        await dispatch(
            role_env,
            ident="two",
            description=json.dumps(
                {
                    "task_id": "two",
                    "role": "searcher",
                    "objective": "facts",
                    "as_of": "2026-04-30",
                }
            ),
        )


def test_verified_announcement_date_is_bound_to_exact_source_and_cutoff():
    from research_copilot.subagents.source_policy import assess_source

    record = {
        "kind": "original_text",
        "content_hash": "a" * 64,
        "raw": {"qualification": {"relevant": True}},
    }
    proof = {
        "content_hash": "a" * 64,
        "published_at": "2025-08-13",
        "verified_by": "operator",
        "official_url": "https://www.sse.com.cn/disclosure/example.pdf",
    }
    assert assess_source(record, "2025-09-30", publication=proof)["eligible"]
    assert not assess_source(record, "2025-08-01", publication=proof)["eligible"]
    assert not assess_source(
        record, "2025-09-30", publication={**proof, "content_hash": "b" * 64}
    )["eligible"]
    assert not assess_source(record, "2025-09-30")["eligible"]


def test_rmb_yuan_display_accepts_unscaled_yuan_but_rejects_larger_denominations():
    import hashlib
    from research_copilot.subagents.claim_validation import validate_claims

    metric = {"value": "123.45", "unit": "人民币元", "display_precision": 2}
    for suffix, valid in [
        ("元", True),
        ("人民币元", True),
        ("亿元", False),
        ("美元", False),
    ]:
        text = "合并报表以人民币元列示，季度营业收入123.45" + suffix
        manifest = {
            "body_hash": hashlib.sha256(text.encode()).hexdigest(),
            "claims": [
                {
                    "start": 0,
                    "end": len(text),
                    "quote": text,
                    "metric_ids": ["m"],
                    "evidence_ids": [],
                }
            ],
        }
        assert (not validate_claims(text, manifest, {"m": metric}, {}, [])) == valid
