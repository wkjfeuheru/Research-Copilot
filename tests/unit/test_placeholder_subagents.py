"""Default role registration must be callable without performing role business work."""

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from tests.unit.test_business import ControlRepository
from tests.unit.test_orchestration import ScriptModel, call


def make_factory(tmp_path, role, **overrides):
    from research_copilot.contracts import ArtifactRef
    from research_copilot.orchestration.factory import SupervisorFactory
    from research_copilot.runtime.budget import TokenBudget
    from research_copilot.runtime.business import BusinessCoordinator
    from research_copilot.workspace import bundled_resources

    repository = ControlRepository()
    plan = ArtifactRef(artifact_id="plan", version="1", content_hash="a" * 64)
    analysis = ArtifactRef(artifact_id="analysis", version="1", content_hash="b" * 64)
    draft = ArtifactRef(artifact_id="draft", version="1", content_hash="c" * 64)
    repository.state.artifacts = {"plan": plan, "analysis": analysis, "report": draft}
    repository.state.approved = {"plan": plan, "analysis": analysis}
    business = BusinessCoordinator(repository)
    budget = TokenBudget(limit=1000000, budget_id="budget-1")

    class Store:
        async def load_budget(self, context):
            return budget

    usage = {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}
    models = {
        name: ScriptModel(script=[AIMessage(content="must not execute")])
        for name in ("searcher", "analysist", "writer", "reviewer")
    }
    models["supervisor"] = ScriptModel(
        script=[
            call(
                "task", {"subagent_type": role, "description": "perform role task"}
            ).model_copy(update={"usage_metadata": usage}),
            AIMessage(content="role unavailable", usage_metadata=usage),
        ]
    )
    # Scaffolds are now an explicit compatibility mode; production defaults to business roles.
    overrides.setdefault("role_tools", {})
    factory = SupervisorFactory(
        models=models,
        store=Store(),
        business=business,
        checkpointer=InMemorySaver(),
        workspace_root=tmp_path / "workspace",
        resources_root=bundled_resources(),
        **overrides,
    )
    return factory, models, business, repository, budget


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["searcher", "analysist", "writer", "reviewer"])
async def test_historical_scaffold_factory_cannot_execute(tmp_path, role):
    from research_copilot.runtime.control import RunControl
    from research_copilot.runtime.session import SessionRecord

    factory, models, business, repository, budget = make_factory(tmp_path, role)
    record = SessionRecord(
        "report-1", "actor-1", "v2", "policy-1", "budget-1", run_id="run-1"
    )
    before = repository.state.model_dump()
    with pytest.raises(PermissionError, match="read-only"):
        await factory(record, RunControl())
    assert repository.state.model_dump() == before
    assert all(model.position == 0 for model in models.values())
    assert budget.charged_tokens == 0


def test_unknown_role_configuration_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="role"):
        make_factory(tmp_path, "searcher", role_tools={"typo": []})
