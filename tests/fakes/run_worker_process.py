"""Actual queue/session/checkpoint worker with a blocked, local synthetic model."""

import argparse
import asyncio
import os
from pathlib import Path

from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import Field

from research_copilot.llm import MeteredChatModel
from research_copilot.runtime.loop import run_async
from research_copilot.runtime.run_queue import RunQueue
from research_copilot.runtime.run_worker import RunWorker
from research_copilot.runtime.session import SessionService
from research_copilot.runtime.storage import postgres_runtime
from tests.unit.test_orchestration import ScriptModel


async def main(directory):
    class BlockingModel(ScriptModel):
        directory: object = Field(exclude=True)

        async def _agenerate(self, messages, **kwargs):
            (self.directory / "called").write_text(str(os.getpid()), encoding="utf-8")
            await asyncio.Event().wait()

    async with postgres_runtime(os.environ["RC_TEST_DATABASE_URL"]) as (store, saver):

        def factory(record, control):
            async def node(state):
                model = MeteredChatModel.wrap(
                    BlockingModel(script=[], directory=directory),
                    budget=await store.load_budget(service._context(record)),
                    max_input_tokens=5000,
                    max_output_tokens=100,
                    before_request=control.acheck,
                )
                return {"messages": [await model.ainvoke(state["messages"])]}

            graph = StateGraph(MessagesState)
            graph.add_node("model", node)
            graph.add_edge(START, "model")
            graph.add_edge("model", END)
            return graph.compile(checkpointer=saver)

        service = SessionService(
            store=store, agent_factory=factory, policy_version="policy-1"
        )
        worker = RunWorker(
            RunQueue(store.engine), service, lease_seconds=2, poll_interval=0.05
        )
        await worker.start()
        try:
            (directory / "ready").write_text(str(os.getpid()), encoding="utf-8")
            await asyncio.Event().wait()
        finally:
            await worker.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    run_async(main(parser.parse_args().directory))
