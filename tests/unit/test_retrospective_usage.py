from datetime import UTC, datetime
import pytest

from langchain_core.messages import AIMessage

from research_copilot.runtime.experience import (
    LLMExperienceExtractor,
    RetrospectiveWorker,
    enqueue_delivery,
    retrospective_jobs,
)
from tests.unit.test_experience import _store, _refs, _delivered_state
from tests.unit.test_orchestration import context


@pytest.mark.parametrize(
    "first_content", ["invalid JSON", "", '{"experiences":[{"bad":true}]}']
)
async def test_invalid_json_attempt_usage_survives_retry_and_success(
    tmp_path, first_content
):
    store, engine = await _store(tmp_path)
    plan, analysis, report = _refs()
    state = _delivered_state(report, analysis, plan)
    state.approved["report"] = report
    await store.mutate_control(
        context(), lambda current: current.__dict__.update(state.__dict__)
    )
    async with engine.begin() as connection:
        await enqueue_delivery(
            connection,
            report_id="report-1",
            report_version="1",
            report_artifact_id=report.artifact_id,
            report_content_hash=report.content_hash,
            actor_id="actor-1",
        )

    class Model:
        profile = {}
        model_name = "offline"
        calls = 0

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            return AIMessage(
                content=first_content if self.calls == 1 else '{"experiences":[]}',
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": self.calls,
                    "total_tokens": 10 + self.calls,
                },
                response_metadata={"request_id": f"provider-{self.calls}"},
            )

    model = Model()
    worker = RetrospectiveWorker(store, LLMExperienceExtractor(model))
    try:
        assert await worker.run_once()
        failed = (await store.experience.jobs("report-1"))[0]
        assert failed["usage"]["input_tokens"] == 10
        assert (
            failed["usage"]["attempts"][0]["usage"]["provider_request_id"]
            == "provider-1"
        )
        async with engine.begin() as connection:
            await connection.execute(
                retrospective_jobs.update().values(available_at=datetime.now(UTC))
            )
        assert await worker.run_once()
        done = (await store.experience.jobs("report-1"))[0]
        assert done["status"] == "completed"
        assert done["usage"]["input_tokens"] == 20
        assert done["usage"]["output_tokens"] == 3
        assert [row["status"] for row in done["usage"]["attempts"]] == [
            "failed",
            "completed",
        ]
        assert len({row["attempt_id"] for row in done["usage"]["attempts"]}) == 2
        assert (await store.load_budget(context())).calls == 0
    finally:
        await engine.dispose()
