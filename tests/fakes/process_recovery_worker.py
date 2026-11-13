"""Disposable process probe. Uses a fake model and an explicitly supplied test DB."""

import asyncio
import json
import os
import sys
from pathlib import Path

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph import END, START, MessagesState, StateGraph

from research_copilot.contracts import ArtifactRef, RunContext
from research_copilot.llm import MeteredChatModel
from research_copilot.runtime.business import BusinessCoordinator, approval_key
from research_copilot.runtime.loop import run_async
from research_copilot.runtime.storage import postgres_runtime
from tests.unit.test_orchestration import ScriptModel


async def main():
    window, action, root_arg = sys.argv[1:]
    root = Path(root_arg)
    context = RunContext("actor-1", "report-1", "run-1", "budget-1", "policy-1")

    async def stop_here(store, receipt=None):
        payload = {
            "budget": (await store.load_budget(context)).snapshot(),
            "receipt": receipt,
        }
        temporary = root / "ready.tmp"
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(root / "ready.json")
        await asyncio.Event().wait()

    async with postgres_runtime(os.environ["RC_TEST_DATABASE_URL"]) as (store, saver):

        class ProbeModel(ScriptModel):
            async def _agenerate(self, messages, **kwargs):
                with (root / "calls.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps({"provider_request_id": "fake-request-1"}) + "\n"
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                if action == "start" and window == "sent":
                    await stop_here(store)
                return ChatResult(
                    generations=[
                        ChatGeneration(
                            message=AIMessage(
                                content="offline response",
                                usage_metadata={
                                    "input_tokens": 10,
                                    "output_tokens": 2,
                                    "total_tokens": 12,
                                },
                                response_metadata={"request_id": "fake-request-1"},
                            )
                        )
                    ]
                )

        async def node(state):
            if window == "business":
                artifact = ArtifactRef(
                    artifact_id="plan-1", version="1", content_hash="a" * 64
                )
                receipt = await BusinessCoordinator(store).confirm(
                    context,
                    "confirm_plan",
                    artifact,
                    approval_key(context.report_id, "confirm_plan", artifact),
                )
                if action == "start":
                    await stop_here(store, receipt)
                return {
                    "messages": [AIMessage(content=json.dumps(receipt, sort_keys=True))]
                }
            model = MeteredChatModel.wrap(
                ProbeModel(script=[]),
                budget=await store.load_budget(context),
                max_input_tokens=5000,
                max_output_tokens=100,
            )
            response = await model.ainvoke(state["messages"])
            if action == "start" and window == "settled":
                await stop_here(store)
            return {"messages": [response]}

        builder = StateGraph(MessagesState)
        builder.add_node("work", node)
        builder.add_edge(START, "work")
        builder.add_edge("work", END)
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "v2:report-1"}}
        try:
            result = await graph.ainvoke(
                {"messages": [("user", "offline process probe")]}
                if action == "start"
                else None,
                config,
                durability="sync",
            )
            payload = {
                "status": "completed",
                "last_message": result["messages"][-1].content,
            }
        except ValueError as error:
            if action != "resume":
                raise
            payload = {"status": "blocked", "error": str(error)}
        (root / "result.json").write_text(json.dumps(payload), encoding="utf-8")


if __name__ == "__main__":
    run_async(main())
