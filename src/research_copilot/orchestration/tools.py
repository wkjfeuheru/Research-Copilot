"""Version-bound confirmation tools. HITL pauses before these execute."""

from langchain.tools import ToolRuntime, tool

from research_copilot.contracts import ArtifactRef, BusinessService, RunContext
from research_copilot.runtime.business import (
    CollectionOutputConflict,
    ResearchPlan,
    approval_key,
)
from research_copilot.runtime.rework import ReworkTask
from research_copilot.runtime.workflow import CollectionInputConflict


def compact_status(status, material_paths):
    """Model-facing view only; stored/UI business results retain all details."""
    latest = {}
    for role, result in status.get("latest_role_results", {}).items():
        latest[role] = {key: value for key, value in result.items() if key != "facts"}
        if "facts" in result:
            latest[role]["fact_count"] = len(result["facts"])
            latest[role]["fact_index_path"] = material_paths.get(role)
    return {**status, "latest_role_results": latest}


def build_top_tools(business: BusinessService, *, autonomous=False):
    from research_copilot.runtime.autonomy import ReportScope

    @tool
    async def set_report_scope(
        scope: ReportScope, runtime: ToolRuntime[RunContext]
    ) -> dict:
        """Freeze report type, research objective and as_of before any dispatch."""
        ref = await business.set_report_scope(runtime.context, scope)
        return ref.model_dump()

    @tool
    async def ask_user(question: str, runtime: ToolRuntime[RunContext]) -> dict:
        """Pause durably for missing user information; this never grants approval."""
        from langgraph.types import interrupt
        from research_copilot.runtime.session import validate_decisions

        if not question.strip() or len(question) > 2000:
            raise ValueError("Question must contain 1 to 2000 characters")
        if not runtime.tool_call_id:
            raise ValueError("Clarification requires a tool call identity")
        tool_call_id = runtime.tool_call_id
        payload = {"kind": "clarification", "question": question.strip()}
        if repository := getattr(business, "repository", None):
            from research_copilot.runtime.conversation import append_message

            await repository.mutate_control(
                runtime.context,
                lambda s: append_message(
                    s,
                    message_id="question:" + tool_call_id,
                    role="assistant",
                    content=question.strip(),
                    kind="question",
                ),
            )
        answer = interrupt(payload)
        validate_decisions({"answer": payload}, {"answer": answer})
        return answer

    @tool
    async def propose_plan(
        plan: ResearchPlan, runtime: ToolRuntime[RunContext]
    ) -> dict:
        """Save a proposed research plan and obtain its immutable approval reference."""
        try:
            artifact = await business.propose_plan(runtime.context, plan)
        except (CollectionOutputConflict, CollectionInputConflict) as exc:
            return {"error": "collection_requires_derived_output", "message": str(exc)}
        return artifact.model_dump()

    @tool
    async def get_report_status(runtime: ToolRuntime[RunContext]) -> dict:
        """Read authoritative report progress, approved versions and pending gaps."""
        status = await business.get_status(runtime.context)
        paths = {}
        if services := getattr(business, "role_services", None):
            state = await services.repository.read_control(runtime.context)
            for ident, result in state.role_data.get("task_results", {}).items():
                if result.get("facts"):
                    paths[state.dispatches[ident]["role"]] = (
                        f"/workspace/artifacts/{ident}/fact_index.json"
                    )
        return compact_status(status, paths)

    @tool
    async def propose_rework(
        review_id: str, tasks: list[ReworkTask], runtime: ToolRuntime[RunContext]
    ) -> dict:
        """Propose version-bound fixes for current review issues without erasing the report."""
        ref = await business.propose_rework(
            runtime.context, review_id, [t.model_dump() for t in tasks]
        )
        return ref.model_dump()

    async def confirm(action, artifact, runtime):
        context = runtime.context
        key = approval_key(context.report_id, action, artifact)
        return await business.confirm(context, action, artifact, key)

    @tool
    async def confirm_plan(
        artifact: ArtifactRef, runtime: ToolRuntime[RunContext]
    ) -> dict:
        """Request human approval of this exact plan version and content hash."""
        return await confirm("confirm_plan", artifact, runtime)

    @tool
    async def confirm_analysis(
        artifact: ArtifactRef, runtime: ToolRuntime[RunContext]
    ) -> dict:
        """Request human approval of this exact analysis version and content hash."""
        return await confirm("confirm_analysis", artifact, runtime)

    @tool
    async def confirm_rework(
        artifact: ArtifactRef, runtime: ToolRuntime[RunContext]
    ) -> dict:
        """Request human approval of this exact rework plan and content hash."""
        return await confirm("confirm_rework", artifact, runtime)

    @tool
    async def signoff_report(
        artifact: ArtifactRef, runtime: ToolRuntime[RunContext]
    ) -> dict:
        """Request final human signoff of the current reviewed report version."""
        return await confirm("signoff_report", artifact, runtime)

    if autonomous:
        return [
            get_report_status,
            set_report_scope,
            ask_user,
            propose_rework,
            signoff_report,
        ]
    return [
        get_report_status,
        propose_plan,
        propose_rework,
        confirm_plan,
        confirm_analysis,
        confirm_rework,
        signoff_report,
    ]
