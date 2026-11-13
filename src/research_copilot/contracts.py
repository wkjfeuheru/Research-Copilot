"""Contracts across the orchestration/business/role boundaries."""

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROLE_NAMES = ("searcher", "analysist", "writer", "reviewer")
FIELD_KEY_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,79}$"
AUTONOMOUS_ROLES = ROLE_NAMES[:3]
CONFIRMATION_TOOLS = (
    "confirm_plan",
    "confirm_analysis",
    "confirm_rework",
    "signoff_report",
)
Role = Literal["searcher", "analysist", "writer", "reviewer"]


@dataclass(frozen=True, slots=True)
class RunContext:
    actor_id: str
    report_id: str
    run_id: str
    budget_id: str
    policy_version: str


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class RequiredMetric(BaseModel):
    """Plan-level metric requirement; value is optional and pinned only by frozen regression criteria."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str = Field(pattern=FIELD_KEY_PATTERN)
    semantic_type: str = Field(min_length=1, max_length=80)
    unit: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)


class TaskBrief(BaseModel):
    """Frozen per-task contract proposed with the plan and bound at dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(min_length=1)
    role: Role
    objective: str = Field(min_length=1)
    as_of: str
    input_refs: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    feedback_refs: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list, max_length=100)
    optional_fields: list[str] = Field(default_factory=list, max_length=100)
    required_metrics: list[RequiredMetric] = Field(default_factory=list, max_length=10)
    max_source_calls: int = Field(default=8, ge=1, le=30)
    max_no_progress_calls: int = Field(default=2, ge=1, le=5)
    max_facts: int = Field(default=15, ge=1, le=100)
    execution_purposes: list[str] = Field(default_factory=list, max_length=10)
    max_executions: int = Field(default=2, ge=1, le=10)
    calculation_mode: Literal["declarative", "custom"] = Field(
        default="declarative",
        description="Standard arithmetic uses run_metric_specs; custom is for charts or unsupported methods.",
    )
    entity_terms: list[str] = Field(default_factory=list, max_length=10)
    topic_terms: list[str] = Field(default_factory=list, max_length=10)
    problem_type: str | None = Field(default=None, max_length=64)
    repair_of: str | None = Field(default=None, max_length=128)

    @field_validator("as_of")
    @classmethod
    def valid_as_of(cls, value):
        # Lazy import: the policy module owns the research-calendar rules and
        # must not create an import cycle with the root contracts.
        from research_copilot.subagents.source_policy import parse_research_as_of

        return parse_research_as_of(value).isoformat()

    @field_validator("problem_type")
    @classmethod
    def valid_problem_type(cls, value):
        if value is None:
            return value
        from research_copilot.runtime.experience import PROBLEM_TYPE_TAGS

        if value not in PROBLEM_TYPE_TAGS:
            raise ValueError("Unknown problem_type tag")
        return value


class RoleResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Role
    status: Literal["completed", "needs_input", "unavailable"]
    summary: str = Field(min_length=1)
    references: list[ArtifactRef]
    gaps: list[str]
    used_experience_ids: list[str] = Field(default_factory=list, max_length=3)


class UnavailableReviewResult(RoleResult):
    """An unimplemented reviewer has no reviewed version or review decision."""

    role: Literal["reviewer"] = "reviewer"
    status: Literal["unavailable"] = "unavailable"
    references: list[ArtifactRef] = Field(default_factory=list, max_length=0)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["passed", "changes_requested", "rejected", "stale"]
    summary: str = Field(min_length=1)
    reviewed: ArtifactRef
    issues: list[dict[str, Any]]
    comments: list[str]


@dataclass(frozen=True, slots=True)
class Dispatch:
    dispatch_id: str
    role: Role
    write_prefix: str


class BusinessService(Protocol):
    """Authoritative services supplied by the host, never by model arguments.

    authorize_dispatch enforces confirmed plan/analysis/rework prerequisites.
    accept_result validates referenced versions/hashes and records the result;
    reviewer results must be checked against the frozen review snapshot here.
    confirm rechecks current artifact, prerequisites and authenticated actor,
    atomically persists approval + receipt, and replays the same receipt by the
    business idempotency key. An unavailable implementation must raise.
    """

    async def authorize_dispatch(
        self, context: RunContext, dispatch: Dispatch
    ) -> None: ...
    async def set_report_scope(
        self, context: RunContext, scope: Any
    ) -> ArtifactRef: ...
    async def propose_plan(self, context: RunContext, plan: Any) -> ArtifactRef: ...
    async def propose_rework(
        self, context: RunContext, review_id: str, tasks: list
    ) -> ArtifactRef: ...
    async def accept_result(
        self, context: RunContext, dispatch: Dispatch, result: RoleResult | ReviewResult
    ) -> RoleResult | ReviewResult: ...
    async def get_status(self, context: RunContext) -> dict[str, Any]: ...
    async def confirm(
        self,
        context: RunContext,
        action: str,
        artifact: ArtifactRef,
        idempotency_key: str,
    ) -> dict[str, Any]: ...
