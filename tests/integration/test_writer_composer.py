from types import SimpleNamespace

import pytest

from research_copilot.runtime.control import active_dispatch
from research_copilot.subagents.writer import build_writing_tools
from tests.integration.test_searcher import dispatch
from tests.integration.test_writer import confirmed_analysis
from tests.unit.test_orchestration import context


@pytest.mark.asyncio
async def test_composer_publishes_bound_title_and_preserves_rejected_candidate(
    role_env,
):
    analysis_id = await confirmed_analysis(role_env, quantitative=True)
    services, _, store = role_env
    scope = await dispatch(role_env, "writer", "compose")
    token = active_dispatch.set(scope)
    try:
        compose = next(
            t for t in build_writing_tools(services) if t.name == "compose_report"
        )
        runtime = SimpleNamespace(context=context(), tool_call_id="compose-valid")
        texts = [
            "Source-supported observations and limitations. " * 9 + "[^E-000001]"
            for _ in range(7)
        ]
        result = await compose.coroutine(
            title="变化{{metric:M-" + "b" * 16 + "}}",
            section_texts=texts,
            analysis_id=analysis_id,
            runtime=runtime,
        )
        state = await store.read_control(context())
        payload = state.role_data["objects"][result["draft_id"]]["payload"]
        assert "0.20" in payload["document"]
        assert payload["claims"]["claims"][0]["metric_ids"] == ["M-" + "b" * 16]
        runtime.tool_call_id = "compose-bad"
        with pytest.raises(ValueError, match="Expected 7"):
            await compose.coroutine(
                title="bad",
                section_texts=["wrong sections"],
                analysis_id=analysis_id,
                runtime=runtime,
            )
        state = await store.read_control(context())
        assert state.role_data["writer_candidates"][scope.dispatch_id][
            "section_texts"
        ] == ["wrong sections"]
        assert state.artifacts["report"].version == "1"
    finally:
        active_dispatch.reset(token)
