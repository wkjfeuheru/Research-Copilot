"""Business results explicitly mapped to the established supervisor contract."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from research_copilot.contracts import ArtifactRef, FIELD_KEY_PATTERN
from research_copilot.subagents.comparison import ComparisonBasis


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolFailure(Contract):
    code: str
    message: str
    retryable: bool = False


class BusinessToolResult(Contract):
    ok: bool
    data: dict = Field(default_factory=dict)
    receipt_id: str | None = None
    error: ToolFailure | None = None


class BusinessInputError(ValueError):
    """Correctable candidate error; authorization/integrity failures use other types."""


# TaskBrief/RequiredMetric live in the root contracts so the runtime plan schema
# can bind them without a reverse dependency on this package.
from research_copilot.contracts import (  # noqa: E402
    RequiredMetric as RequiredMetric,
    TaskBrief as TaskBrief,
)


class Gap(Contract):
    description: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    suggested_role: Literal["searcher", "analysist", "writer", "supervisor"]


class TaskResult(Contract):
    task_id: str = Field(min_length=1)
    status: Literal["completed", "partial", "blocked"]
    summary: str = Field(min_length=1, max_length=1200)
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    used_experience_ids: list[str] = Field(default_factory=list, max_length=3)


class EvidenceFact(Contract):
    key: str = Field(pattern=FIELD_KEY_PATTERN)
    evidence_id: str
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    pointer: str = ""
    quote: str = Field(default="", max_length=1000)
    raw_value: str = Field(min_length=1, max_length=1000)
    unit: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    period: str = Field(min_length=1)
    entity: str = Field(min_length=1)
    basis: str = Field(min_length=1)
    scale: str = "1"


class SearchResult(TaskResult):
    coverage: list[str] = Field(default_factory=list)
    source_warnings: list[str] = Field(default_factory=list)
    facts: list[EvidenceFact] = Field(default_factory=list, max_length=100)
    stop_reason: (
        Literal["required_complete", "source_exhausted", "no_progress", "blocked"]
        | None
    ) = None


class MetricSpec(Contract):
    """Declarative metric request; the host computes the value, the model never does."""

    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
    semantic_type: str = Field(min_length=1, max_length=80)
    formula_id: str = Field(min_length=1, max_length=40)
    numerator_basis: str = Field(min_length=1, max_length=200)
    denominator_basis: str = Field(min_length=1, max_length=200)
    unit: str = Field(min_length=1, max_length=80)
    input_keys: list[str] = Field(min_length=1, max_length=10)
    description: str = Field(default="", max_length=2000)
    display_precision: int = Field(ge=0, le=8)
    is_estimate: bool = False
    comparison: ComparisonBasis | None = None


class AssumptionSpec(Contract):
    """Analyst assumption consumed by metric specs; never treated as evidence."""

    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
    value: str | list[str] = Field(min_length=1)
    basis: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)


class FactReference(Contract):
    key: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    period: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    scale: str = "1"


class AnalysisClaim(Contract):
    claim_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)
    metric_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    fact_refs: list[FactReference] = Field(default_factory=list)
    required_in_report: bool = True
    limitations: list[str] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)


class AnalysisCandidate(Contract):
    schema_version: int = Field(default=2, ge=2, le=2)
    summary: str = Field(min_length=1)
    conclusions: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    metric_ids: list[str] = Field(default_factory=list)
    chart_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    claims: list[AnalysisClaim] = Field(default_factory=list, max_length=100)


class SectionClaim(Contract):
    """Writer claim with a unique quote; the host locates the body offsets."""

    quote: str = Field(min_length=1, max_length=1000)
    metric_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    fact_refs: list[FactReference] = Field(default_factory=list)
    conclusion_id: str | None = None


class ReportSectionClaims(Contract):
    heading: str = Field(min_length=1, max_length=200)
    claims: list[SectionClaim] = Field(default_factory=list, max_length=50)


class ReportFileClaims(Contract):
    sections: list[ReportSectionClaims] = Field(max_length=12)


class ReportSection(ReportSectionClaims):
    text: str = Field(min_length=1, max_length=8000)


class AnalysisResult(TaskResult):
    analysis_id: str | None = None
    metric_ids: list[str] = Field(default_factory=list)
    chart_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    candidate: AnalysisCandidate | None = None


class WritingResult(TaskResult):
    draft_id: str | None = None
    draft_version: int | None = None
    report_type: str
    analysis_ids: list[str] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    title: str | None = None
    sections: list[ReportSection] = Field(default_factory=list, max_length=12)


RESULT_TYPES = {
    "searcher": SearchResult,
    "analysist": AnalysisResult,
    "writer": WritingResult,
}
