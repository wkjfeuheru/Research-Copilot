import pytest

from research_copilot.runtime.budget import TokenBudget


@pytest.mark.parametrize("sent,usage", [(False, None), (True, None), (True, 12)])
async def test_pending_recovery_preserves_identity_and_audited_usage(sent, usage):
    budget = TokenBudget(limit=1000, budget_id="b")
    key = await budget.reserve(100, operation_id="stable-node")
    if sent:
        await budget.mark_sent(key)
    recovered = TokenBudget(**budget.snapshot())
    await recovered.recover_pending(
        key, usage=usage, actor_id="operator", note="checked provider receipt"
    )
    snap = recovered.snapshot()
    assert not snap["pending"]
    assert snap["charged_tokens"] == ((100 if usage is None else usage) if sent else 0)
    assert snap["calls"] == (1 if sent else 0)
    assert snap["requests"][key]["operation_id"] == "stable-node"
    assert snap["requests"][key]["recovery"]["actor_id"] == "operator"
    await recovered.recover_pending(
        key, usage=usage, actor_id="operator", note="checked provider receipt"
    )
    assert recovered.snapshot() == snap
    if sent:
        with pytest.raises(ValueError, match="recovery|retry"):
            await recovered.reserve(100, operation_id="stable-node")
        await recovered.authorize_retry(
            key, actor_id="operator", note="accept another charged attempt"
        )
    retry = await recovered.reserve(100, operation_id="stable-node")
    assert retry != key
    if sent:
        assert recovered.requests[retry]["retry_of"] == key
        assert recovered.charged_tokens == snap["charged_tokens"]
        with pytest.raises(ValueError, match="recovery|retry"):
            await recovered.reserve(100, operation_id="stable-node")


async def test_settled_response_without_checkpoint_does_not_trigger_another_paid_call():
    from langchain_core.messages import AIMessage
    from research_copilot.llm import MeteredChatModel
    from tests.unit.test_orchestration import ScriptModel

    inner = ScriptModel(
        script=[
            AIMessage(
                content="response",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                },
            )
        ]
    )
    budget = TokenBudget(limit=10000, budget_id="b")
    model = MeteredChatModel.wrap(
        inner, budget=budget, max_input_tokens=5000, max_output_tokens=100
    )
    config = {
        "metadata": {
            "langgraph_checkpoint_ns": "model:stable-task",
            "langgraph_node": "model",
            "langgraph_step": 1,
        }
    }
    await model.ainvoke("question", config)
    with pytest.raises(ValueError, match="recovery|retry"):
        await model.ainvoke("question", config)
    assert inner.position == 1 and budget.calls == 1


async def test_legacy_sent_pending_requires_audited_recovery_and_retry_authorization():
    budget = TokenBudget(limit=1000, budget_id="b")
    key = await budget.reserve(100)
    await budget.mark_sent(key)
    await budget.recover_pending(
        key, actor_id="operator", note="legacy interrupted request"
    )
    with pytest.raises(ValueError, match="retry"):
        await budget.reserve(100, operation_id="new-node")
    await budget.authorize_retry(key, actor_id="operator", note="legacy retry approved")
    retry = await budget.reserve(100, operation_id="new-node")
    assert budget.requests[retry]["retry_of"] == key
    assert budget.charged_tokens == 100


async def test_reloaded_legacy_pending_blocks_new_reservations_until_recovered():
    budget = TokenBudget(limit=1000, budget_id="b")
    key = await budget.reserve(100)
    restored = TokenBudget(**budget.snapshot())
    with pytest.raises(ValueError, match="recovery"):
        await restored.reserve(100, operation_id="new-node")
    await restored.recover_pending(key, actor_id="operator", note="never marked sent")
    await restored.reserve(100, operation_id="new-node")
