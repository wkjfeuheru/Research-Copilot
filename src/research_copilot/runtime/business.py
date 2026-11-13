"""Supervisor-owned planning, prerequisite gates and version-bound receipts.

The repository runs each mutation in one transaction. Role-specific evidence,
analysis and writing services publish immutable references through that same
repository; this module does not implement their internal business logic.
"""

import hashlib
import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from research_copilot.contracts import (
    ArtifactRef,
    Dispatch,
    RoleResult,
    ReviewResult,
    RunContext,
    TaskBrief,
)

from research_copilot.runtime.identity import RUNTIME_VERSION, confirmation_receipt_id

ACTION_KIND = {
    "confirm_plan": "plan",
    "confirm_analysis": "analysis",
    "confirm_rework": "rework",
    "signoff_report": "report",
}


class CollectionOutputConflict(ValueError):
    """A proposed analysis output was also made a prerequisite for collection."""


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    report_type: Literal[
        "company_report", "flash_comment", "industry_report", "strategy_report"
    ]
    objective: str = Field(min_length=1, max_length=8000)
    analysis_execution_limit: int = Field(default=2, ge=1, le=10)
    facts_per_task_limit: int = Field(default=15, ge=1, le=100)
    execution_purposes: list[str] = Field(default_factory=list, max_length=10)
    # Typed task contracts: validated at proposal time, approved with the plan,
    # and bound authoritatively at dispatch.
    tasks: list[TaskBrief] = Field(min_length=1, max_length=6)


class ControlState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)
    approved: dict[str, ArtifactRef] = Field(default_factory=dict)
    plans: list[dict] = Field(default_factory=list)
    grants: dict[str, str] = Field(default_factory=dict)
    receipts: dict[str, dict] = Field(default_factory=dict)
    dispatches: dict[str, dict] = Field(default_factory=dict)
    results: dict[str, dict] = Field(default_factory=dict)
    reviewed_report: ArtifactRef | None = None
    role_data: dict[str, Any] = Field(default_factory=dict)


class ControlRepository(Protocol):
    async def read_control(self, context: RunContext) -> ControlState: ...
    async def mutate_control(self, context: RunContext, operation):
        """Authenticate context, lock current state, run pure callback, commit atomically."""
        ...


def approval_key(report_id: str, action: str, artifact: ArtifactRef) -> str:
    identity = json.dumps(
        [report_id, action, artifact.model_dump()],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _current(state, kind, artifact):
    if state.artifacts.get(kind) != artifact:
        raise ValueError(f"stale {kind} artifact version/hash")


def _approved(state, kind):
    current = state.artifacts.get(kind)
    if current is None or state.approved.get(kind) != current:
        raise PermissionError(f"Current {kind} requires human confirmation")


class BusinessCoordinator:
    def __init__(self, repository: ControlRepository, *, review_authorizer=None):
        self.repository = repository
        self.review_authorizer = review_authorizer
        self.role_services = None

    async def set_report_scope(self, context, scope):
        from research_copilot.runtime.autonomy import freeze_scope

        return await self.repository.mutate_control(
            context, lambda state: freeze_scope(state, context.report_id, scope)
        )

    async def propose_plan(
        self, context: RunContext, plan: ResearchPlan
    ) -> ArtifactRef:
        content_hash = hashlib.sha256(plan.model_dump_json().encode()).hexdigest()

        def save(state):
            if state.role_data.get("workflow_version") == "products-v1":
                from research_copilot.runtime.workflow import validate_product_plan

                tasks = validate_product_plan(plan.tasks)
                analysis = next(t for t in tasks if t.role == "analysist")
                outputs = set(analysis.required_fields) | {
                    m.key for m in analysis.required_metrics
                }
                collected = {
                    key
                    for task in tasks
                    if task.role == "searcher"
                    for key in task.required_fields
                }
                if overlap := outputs & collected:
                    raise CollectionOutputConflict(
                        "采集必填字段只能是原文操作数。下列键同时被声明为分析输出："
                        + ", ".join(sorted(overlap))
                        + "。请把累计数、基期数或分子分母列入采集，将计算结果仅列入分析。"
                    )
            from research_copilot.runtime.rework import pending_review

            if pending_review(state) or "rework" in state.artifacts:
                raise ValueError(
                    "use_propose_rework: preserve the current report and analysis"
                )
            previous = state.artifacts.get("plan")
            if previous and previous.content_hash == content_hash:
                return previous
            if "report" in state.approved:
                raise PermissionError("Signed reports cannot be replanned")
            artifact = ArtifactRef(
                artifact_id=f"plan:{context.report_id}",
                version=str(len(state.plans) + 1),
                content_hash=content_hash,
            )
            state.plans.append(
                {"artifact": artifact.model_dump(), "plan": plan.model_dump()}
            )
            state.artifacts["plan"] = artifact
            # New plan invalidates downstream inputs and old human grants.
            state.approved.clear()
            state.grants.clear()
            state.artifacts = {"plan": artifact}
            state.reviewed_report = None
            return artifact

        return await self.repository.mutate_control(context, save)

    async def propose_rework(self, context, review_id, tasks):
        from research_copilot.runtime.rework import propose

        return await self.repository.mutate_control(
            context, lambda state: propose(state, review_id, tasks)
        )

    async def recover_rework(self, context, review_id):
        """Host-only explicit repair; never exposed as an autonomous model tool."""
        from research_copilot.runtime.rework import recover

        if self.role_services is None:
            raise ValueError("Recovery requires immutable artifact storage")
        return await self.repository.mutate_control(
            context,
            lambda state: recover(
                state, review_id, self.role_services.report_root(context)
            ),
        )

    async def get_status(self, context):
        state = await self.repository.read_control(context)
        budget = (
            (await self.repository.load_budget(context)).status()
            if hasattr(self.repository, "load_budget")
            else None
        )
        return self.project_status(context, state, budget)

    @staticmethod
    def project_status(context, state, budget=None):
        from research_copilot.runtime.rework import status as rework_status

        draft = state.artifacts.get("report")
        latest = {}
        for ident, result in state.role_data.get("task_results", {}).items():
            latest[state.dispatches[ident]["role"]] = result
        payload = {
            "report_id": context.report_id,
            "runtime_version": RUNTIME_VERSION,
            "rework": rework_status(state),
            "artifacts": {
                kind: ref.model_dump() for kind, ref in state.artifacts.items()
            },
            "approved_versions": {
                kind: ref.model_dump() for kind, ref in state.approved.items()
            },
            "approved": draft is not None and state.approved.get("report") == draft,
            "receipts": list(state.receipts.values()),
            "latest_role_results": latest,
            "latest_review": next(
                reversed(state.role_data.get("reviews", {}).values()), None
            ),
        }

        if budget is not None:
            payload["budget"] = budget
        from research_copilot.runtime.autonomy import autonomous, progress

        if autonomous(state):
            payload.update(progress(state))
            payload["workflow"] = {
                "version": "products-v2",
                "phase": "complete"
                if payload["approved"]
                else "blocked"
                if state.role_data.get("last_run_error")
                else "running",
                "reason": state.role_data.get("last_run_error", {}).get("message"),
            }
        if state.role_data.get("workflow_version") == "products-v1":
            from research_copilot.runtime.workflow import decide_next, WorkflowBlocked

            try:
                next_action = decide_next(state, context.report_id)
                payload["workflow"] = {
                    "version": "products-v1",
                    "phase": next_action["kind"],
                    "next_action": next_action.get("name"),
                    "role": next_action.get("args", {}).get("subagent_type"),
                }
            except WorkflowBlocked as exc:
                payload["workflow"] = {
                    "version": "products-v1",
                    "phase": "blocked",
                    "reason": str(exc),
                }
        return payload

    async def authorize_dispatch(self, context: RunContext, dispatch: Dispatch):
        def authorize(state):
            from research_copilot.runtime.autonomy import autonomous

            if autonomous(state):
                # prepare_dispatch performs admission and scope registration in
                # ONE transaction. This call must not reserve a partial binding.
                return
            _approved(state, "plan")
            if dispatch.role == "writer":
                _approved(state, "analysis")
            if dispatch.role == "reviewer" and "report" not in state.artifacts:
                raise PermissionError("A frozen report is required for review")
            if "rework" in state.artifacts:
                _approved(state, "rework")
            binding = {
                "role": dispatch.role,
                "inputs": {
                    kind: ref.model_dump() for kind, ref in state.artifacts.items()
                },
            }
            from research_copilot.runtime.rework import validate_dispatch

            validate_dispatch(state, binding)
            existing = state.dispatches.get(dispatch.dispatch_id)
            if existing and dispatch.role == "reviewer":
                # A resumed reviewer must inspect its original frozen version and
                # return stale if the current draft changed while awaiting feedback.
                return
            if existing and existing != binding:
                raise ValueError("Dispatch inputs changed during replay")
            state.dispatches[dispatch.dispatch_id] = binding

        await self.repository.mutate_control(context, authorize)

    async def accept_result(self, context, dispatch, result: RoleResult | ReviewResult):
        def accept(state):
            binding = state.dispatches.get(dispatch.dispatch_id)
            role = "reviewer" if isinstance(result, ReviewResult) else result.role
            if not binding or binding["role"] != role:
                raise PermissionError("Result is not bound to an authorized dispatch")
            if isinstance(result, ReviewResult):
                record = state.role_data.get("reviews", {}).get(
                    "review-" + dispatch.dispatch_id
                )
                if self.role_services is not None and (
                    not record
                    or record["reviewed"] != result.reviewed.model_dump()
                    or record["outcome"] != result.status
                ):
                    raise PermissionError(
                        "Reviewer result requires a committed review receipt"
                    )
                if result.status != "stale":
                    _current(state, "report", result.reviewed)
                if binding["inputs"].get("report") != result.reviewed.model_dump():
                    raise ValueError(
                        "Review result differs from the frozen dispatch snapshot"
                    )
                if result.status != "stale":
                    state.reviewed_report = (
                        result.reviewed if result.status == "passed" else None
                    )
                if result.status == "changes_requested" and record:
                    payload = {
                        "review_id": record["review_id"],
                        "reviewed": record["reviewed"],
                        "issues": record["issues"],
                        "feedback": record["feedback"],
                    }
                    content = json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    ref = ArtifactRef(
                        artifact_id="rework:" + record["review_id"],
                        version=result.reviewed.version,
                        content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    )
                    state.artifacts["rework"] = ref
                    state.approved.pop("rework", None)
                    state.role_data.setdefault("rework", {})[ref.artifact_id] = payload
            else:
                from research_copilot.runtime.rework import validate_dispatch

                validate_dispatch(state, binding)
                if result.status != "completed" and not result.gaps:
                    raise ValueError("Incomplete role results must identify their gaps")
                for ref in result.references:
                    registered = [
                        ArtifactRef.model_validate(item["ref"])
                        for item in state.role_data.get("objects", {}).values()
                    ]
                    registered.extend(
                        ArtifactRef(
                            artifact_id=ident,
                            version="1",
                            content_hash=item["content_hash"],
                        )
                        for ident, item in state.role_data.get("evidence", {}).items()
                        if ident
                        in state.role_data.get("scopes", {})
                        .get(dispatch.dispatch_id, {})
                        .get("evidence_ids", [])
                    )
                    if ref not in state.artifacts.values() and ref not in registered:
                        raise ValueError("Unknown or stale result reference")
            state.results[dispatch.dispatch_id] = result.model_dump()
            return result

        return await self.repository.mutate_control(context, accept)

    async def validate_resume(self, context, pending, decisions):
        # Reviewer identity, snapshot and feedback validation belongs to its service.
        for ident, payload in pending.items():
            if payload.get("kind") == "reviewer_deep_review":
                if self.review_authorizer is None:
                    raise RuntimeError("Reviewer decision service is unavailable")
                await self.review_authorizer(context, payload, decisions[ident])

        def grant(state):
            for ident, payload in pending.items():
                if payload.get("kind") == "clarification":
                    from research_copilot.runtime.conversation import append_message

                    append_message(
                        state,
                        message_id="answer:" + ident,
                        role="user",
                        content=decisions[ident]["answer"].strip(),
                    )
                for action, decision in zip(
                    payload.get("action_requests", []),
                    decisions[ident].get("decisions", []),
                    strict=True,
                ):
                    kind = ACTION_KIND[action["name"]]
                    artifact = ArtifactRef.model_validate(action["args"]["artifact"])
                    _current(state, kind, artifact)
                    if decision["type"] == "approve":
                        state.grants[
                            approval_key(context.report_id, action["name"], artifact)
                        ] = context.actor_id

        await self.repository.mutate_control(context, grant)

    async def confirm(self, context, action, artifact, idempotency_key):
        kind = ACTION_KIND[action]
        expected_key = approval_key(context.report_id, action, artifact)
        if idempotency_key != expected_key:
            raise ValueError("Invalid business idempotency key")
        effects = []
        experience_ready = bool(getattr(self.repository, "experience_ready", False))

        def commit(state, budget_status=None):
            _current(state, kind, artifact)
            if state.approved.get(kind) == artifact and (
                receipt := state.receipts.get(expected_key)
            ):
                return receipt
            if state.grants.get(expected_key) != context.actor_id:
                raise PermissionError("A validated human approval is required")
            if kind == "analysis" and (rework := state.artifacts.get("rework")):
                payload = state.role_data["rework"][rework.artifact_id]
                if any(
                    t["role"] in {"searcher", "analysist"}
                    for t in payload.get("tasks", [])
                ) and artifact.model_dump() == payload.get("base_analysis_ref"):
                    raise PermissionError(
                        "Analysis rework requires a newly submitted analysis"
                    )
            if kind == "report":
                from research_copilot.runtime.signoff import signoff_error

                if error := signoff_error(state, budget_status):
                    raise PermissionError(error)
            elif kind != "plan":
                _approved(state, "plan")
                if kind == "rework":
                    from research_copilot.runtime.rework import validate_base

                    validate_base(
                        state, state.role_data["rework"][artifact.artifact_id]
                    )
            state.approved[kind] = artifact
            receipt = {
                "receipt_id": confirmation_receipt_id(expected_key),
                "action": action,
                "artifact": artifact.model_dump(),
                "actor_id": context.actor_id,
            }
            state.receipts[expected_key] = receipt
            state.grants.pop(expected_key, None)
            if kind == "report" and experience_ready:
                # Same transaction as the approved control row: one job per
                # delivered report version, never for cancelled/failed drafts.
                effects.append(
                    {
                        "kind": "delivery_completed",
                        "report_id": context.report_id,
                        "actor_id": context.actor_id,
                        "artifact": artifact.model_dump(),
                    }
                )
            return receipt

        options = {"include_budget": True} if kind == "report" else {}
        if experience_ready:
            options["effects"] = effects
        return await self.repository.mutate_control(context, commit, **options)
