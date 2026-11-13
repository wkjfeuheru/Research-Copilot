"""Worker startup failure must not close dependencies beneath admitted work."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


async def test_failed_secondary_worker_start_drains_research_before_closing_dependencies(
    monkeypatch, tmp_path
):
    import research_copilot.host as host
    import research_copilot.web.app as web
    import research_copilot.web.charts as charts
    import research_copilot.runtime.experience as experience

    events = []

    @asynccontextmanager
    async def open_session(*args, **kwargs):
        try:
            yield (
                object(),
                object(),
                SimpleNamespace(experience=object(), experience_extractor=object()),
            )
        finally:
            events.append("dependencies.closed")

    class Runs:
        async def start(self):
            events.append("research.started")

        async def aclose(self):
            events.append("research.closed")

    class FailedWorker:
        def __init__(self, *args):
            pass

        async def start(self):
            events.append("retrospective.failed")
            raise RuntimeError("secondary startup failed")

        async def stop(self):
            events.append("retrospective.stopped")

    monkeypatch.setattr(host, "open_session", open_session)
    monkeypatch.setattr(
        web, "WorkbenchBackend", lambda **kwargs: SimpleNamespace(runs=Runs())
    )
    monkeypatch.setattr(charts, "ChartService", lambda services: None)
    monkeypatch.setattr(experience, "RetrospectiveWorker", FailedWorker)
    app = web.build_server_app(
        settings=object(),
        models_path=tmp_path,
        workspace_root=tmp_path,
        actor_id="local",
        policy_version="policy-1",
        token_limit=100,
        mode="auto",
        rules_path=tmp_path,
        rules_approval_path=None,
        execution_policy_path=tmp_path,
        static_dir=None,
    )
    with pytest.raises(RuntimeError, match="secondary startup"):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup failure was ignored")
    assert events == [
        "research.started",
        "retrospective.failed",
        "research.closed",
        "retrospective.stopped",
        "dependencies.closed",
    ]
