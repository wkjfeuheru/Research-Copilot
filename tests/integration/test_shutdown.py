import pytest

from research_copilot.llm import MeteredChatModel
from tests.unit.test_orchestration import ScriptModel, context


async def test_sql_recovery_does_not_resend_a_completed_model_node_after_checkpoint_failure(
    role_env,
):
    from langchain_core.messages import AIMessage
    from langgraph.graph import StateGraph, MessagesState, START, END
    from langgraph.checkpoint.memory import InMemorySaver

    store = role_env[2]
    inner = ScriptModel(
        script=[
            AIMessage(
                content="paid response",
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "total_tokens": 12,
                },
            )
        ]
    )

    async def model_node(state):
        budget = await store.load_budget(context())
        model = MeteredChatModel.wrap(
            inner, budget=budget, max_input_tokens=5000, max_output_tokens=100
        )
        response = await model.ainvoke(state["messages"])
        if inner.position == 1:
            raise RuntimeError("injected after response before checkpoint")
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node("model", model_node)
    builder.add_edge(START, "model")
    builder.add_edge("model", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "durable-model"}}
    with pytest.raises(RuntimeError, match="injected"):
        await graph.ainvoke({"messages": [("user", "research")]}, config)
    with pytest.raises(ValueError, match="retry"):
        await graph.ainvoke(None, config)
    assert inner.position == 1
    budget = await store.load_budget(context())
    key = next(iter(budget.requests))
    assert budget.known_actual_tokens == 12
    await budget.authorize_retry(
        key, actor_id="actor-1", note="explicitly accept another attempt"
    )
    await graph.ainvoke(None, config)
    assert inner.position == 2
    assert (await store.load_budget(context())).known_actual_tokens == 24
