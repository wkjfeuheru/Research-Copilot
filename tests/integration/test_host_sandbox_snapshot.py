"""Actual host binding/runtime lifecycle with frozen, non-executed model fixtures."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from research_copilot import host
from research_copilot.runtime.host_settings import HostSettings
from research_copilot.sandbox.config import SandboxConfig
from tests.integration.test_maintenance_postgres import databases
from tests.unit.test_orchestration import ScriptModel

__all__ = ["databases"]
pytestmark = pytest.mark.db


async def test_real_host_freezes_sandbox_and_requires_explicit_rebinding(
    databases, tmp_path, monkeypatch
):
    settings = HostSettings(_env_file=None, database_url=databases[0])
    options = dict(
        models_path=Path("config/models.example.toml"),
        workspace_root=tmp_path / "workspace",
        policy_version="test-policy",
        mode="test",
    )
    initial = SandboxConfig(concurrency=1)
    changed = SandboxConfig(concurrency=2)
    monkeypatch.setattr(SandboxConfig, "load", lambda: initial)
    bound = await host.bind_host_configuration(
        settings, **options, actor_id="operator", note="Initial test snapshot"
    )
    constructions = []

    @asynccontextmanager
    async def models(config):
        constructions.append(config)
        monkeypatch.setattr(SandboxConfig, "load", lambda: changed)
        yield {
            name: ScriptModel(script=[AIMessage(content="must remain unused")])
            for name in (
                "supervisor",
                "searcher",
                "analysist",
                "writer",
                "reviewer",
                "summarizer",
            )
        }

    monkeypatch.setattr(host, "open_models", models)
    async with host.open_session(settings, **options) as (session, _, _):
        assert session.agent_factory.sandbox_config is initial
    assert len(constructions) == 1
    with pytest.raises(ValueError, match="binding"):
        async with host.open_session(settings, **options):
            pytest.fail("Drifted sandbox was silently accepted")
    assert len(constructions) == 1, "No clients may open before binding is validated"
    rebound = await host.bind_host_configuration(
        settings,
        **options,
        actor_id="operator",
        note="Reviewed changed sandbox; no unfinished work",
        replace=True,
    )
    assert rebound["workspace_id"] == bound["workspace_id"] and rebound["revision"] == 2
    async with host.open_session(settings, **options) as (session, _, _):
        assert session.agent_factory.sandbox_config is changed
    assert len(constructions) == 2
