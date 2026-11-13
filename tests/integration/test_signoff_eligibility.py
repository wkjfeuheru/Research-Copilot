import pytest

from research_copilot.contracts import ArtifactRef
from research_copilot.runtime.business import approval_key
from research_copilot.web.status import status_payload
from tests.unit.test_orchestration import context


async def ready_report(store, *, human=False, hard=False):
    analysis = ArtifactRef(artifact_id="analysis", version="1", content_hash="a" * 64)
    report = ArtifactRef(artifact_id="report", version="1", content_hash="b" * 64)

    def prepare(state):
        state.artifacts.update(analysis=analysis, report=report)
        state.approved["analysis"] = analysis
        state.reviewed_report = report
        state.role_data.setdefault("objects", {})[report.artifact_id] = {
            "payload": {"analysis_ref": analysis.model_dump()},
            "ref": report.model_dump(),
        }
        state.role_data["reviews"] = {
            "r": {
                "review_id": "r",
                "snapshot_id": "s",
                "reviewed": report.model_dump(),
                "outcome": "passed",
                "checks_complete": True,
                "pass_confidence": 0.7 if human else 0.99,
                "decision_source": "human" if human else "automatic",
                "issues": [{"issue_id": "i", "severity": "major", "hard": hard}]
                if human
                else [],
                "feedback": {
                    "decision": "approve",
                    "review_session_id": "r",
                    "snapshot_id": "s",
                    "issue_dispositions": [
                        {
                            "issue_id": "i",
                            "decision": "false_positive",
                            "reason": "verified against source",
                        }
                    ],
                },
            }
        }
        state.grants[approval_key("report-1", "signoff_report", report)] = "actor-1"

    await store.mutate_control(context(), prepare)
    return report


@pytest.mark.parametrize("usage", ["unknown", "pending", "provisional"])
async def test_signoff_rechecks_durable_usage_and_preserves_grant_when_rejected(
    role_env, usage
):
    _, business, store = role_env
    report = await ready_report(store)
    assert (await status_payload(store, "actor-1", "report-1"))["signoff_ready"]
    budget = await store.load_budget(context())
    key = await budget.reserve(100)
    await budget.mark_sent(key)
    if usage != "pending":
        await budget.settle(
            key,
            None if usage == "unknown" else 10,
            details={"usage_provisional": usage == "provisional"},
        )
    approval = approval_key("report-1", "signoff_report", report)
    with pytest.raises(PermissionError, match="usage"):
        await business.confirm(context(), "signoff_report", report, approval)
    current = await store.read_control(context())
    assert approval in current.grants and "report" not in current.approved


async def test_valid_human_false_positive_is_ready_and_signoff_is_idempotent(role_env):
    _, business, store = role_env
    report = await ready_report(store, human=True)
    assert (await status_payload(store, "actor-1", "report-1"))["signoff_ready"]
    key = approval_key("report-1", "signoff_report", report)
    receipt = await business.confirm(context(), "signoff_report", report, key)
    assert await business.confirm(context(), "signoff_report", report, key) == receipt


async def test_hard_issue_cannot_be_overridden_by_human_record(role_env):
    _, business, store = role_env
    report = await ready_report(store, human=True, hard=True)
    assert not (await status_payload(store, "actor-1", "report-1"))["signoff_ready"]
    with pytest.raises(PermissionError, match="review"):
        await business.confirm(
            context(),
            "signoff_report",
            report,
            approval_key("report-1", "signoff_report", report),
        )
