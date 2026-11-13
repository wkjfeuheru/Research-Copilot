"""Post-delivery retrospectives, controlled-tag recall and experience receipts.

Long-term experience is deliberately small: three tables, a controlled vocabulary
shared by writing and recall, and a background worker that only runs after a
human signoff. There is no embedding, vector store or user-preference capture in
this phase.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    case,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from research_copilot.contracts import ArtifactRef, RunContext

logger = logging.getLogger(__name__)

EXPERIENCE_ROLES = ("searcher", "analysist", "writer")
EXPERIENCE_STAGES = ("collect", "analysis", "writing")
STAGE_BY_ROLE = {
    "searcher": "collect",
    "analysist": "analysis",
    "writer": "writing",
}

# Controlled problem-type vocabulary. Retrospective writing and later recall
# share this exact set; free-form categories are mapped deterministically below.
PROBLEM_TYPE_LABELS = {
    "source_quality": "来源与证据质量",
    "numeric_accuracy": "数字、单位与期间",
    "logic_consistency": "逻辑与一致性",
    "structure_format": "结构与格式",
    "disclosure_compliance": "披露与合规",
    "scope_completeness": "范围与完整性",
    "valuation_method": "估值方法与假设",
    "chart_presentation": "图表呈现",
    "writing_quality": "语言与表达",
}
PROBLEM_TYPE_TAGS = tuple(PROBLEM_TYPE_LABELS)

# Order matters: specific method problems win over generic number wording.
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "valuation_method",
        (
            "估值",
            "valuation",
            "dcf",
            "wacc",
            "敏感性",
            "sensitivity",
            "假设",
            "assumption",
            "现金流",
            "cash flow",
        ),
    ),
    (
        "chart_presentation",
        ("图表", "坐标", "轴", "chart", "axis", "visual", "图例", "legend"),
    ),
    (
        "disclosure_compliance",
        (
            "披露",
            "合规",
            "敏感",
            "禁用",
            "承诺",
            "disclosure",
            "compliance",
            "sensitive",
            "prohibit",
        ),
    ),
    (
        "numeric_accuracy",
        (
            "数字",
            "数值",
            "单位",
            "期间",
            "计算",
            "numeric",
            "number",
            "unit",
            "period",
            "arithmetic",
        ),
    ),
    (
        "source_quality",
        (
            "证据",
            "来源",
            "引用",
            "出处",
            "source",
            "evidence",
            "citation",
            "reference",
        ),
    ),
    (
        "logic_consistency",
        (
            "逻辑",
            "一致",
            "推理",
            "矛盾",
            "logic",
            "consisten",
            "reason",
            "contradict",
        ),
    ),
    (
        "structure_format",
        (
            "结构",
            "格式",
            "模板",
            "章节",
            "排版",
            "structure",
            "format",
            "template",
            "section",
        ),
    ),
    (
        "scope_completeness",
        (
            "完整",
            "覆盖",
            "范围",
            "遗漏",
            "缺失",
            "coverage",
            "scope",
            "complete",
            "missing",
        ),
    ),
    (
        "writing_quality",
        ("语言", "表述", "措辞", "写作", "writing", "wording", "language"),
    ),
)


def classify_problem_type(value: Any) -> str | None:
    """Map a free-form review category/label to one controlled tag."""

    text = str(value or "").strip().lower()
    if not text:
        return None
    for tag, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return tag
    return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _lease_clock(connection):
    # Use the database wall clock, not transaction start or a worker's clock.
    return (
        func.clock_timestamp()
        if connection.dialect.name == "postgresql"
        else _utc_now()
    )


def _live_claim(connection, claim):
    return and_(
        retrospective_jobs.c.id == claim["id"],
        retrospective_jobs.c.claim_token == claim["claim_token"],
        retrospective_jobs.c.status == "running",
        retrospective_jobs.c.locked_until > _lease_clock(connection),
    )


def _recovery_required(usage):
    return bool((usage or {}).get("unknown_attempts"))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


class ExperienceExtractionError(ValueError):
    """The extractor returned content that cannot be trusted as an experience."""


class ExperienceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: str
    stage: str
    problem_type: str
    applies_when: str = Field(min_length=10, max_length=2000)
    recommendation: str = Field(min_length=10, max_length=2000)
    evidence_refs: list[str] = Field(min_length=1, max_length=20)

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        if value not in EXPERIENCE_ROLES:
            raise ValueError("未知经验角色：" + value)
        return value

    @field_validator("stage")
    @classmethod
    def _stage(cls, value: str) -> str:
        if value not in EXPERIENCE_STAGES:
            raise ValueError("未知经验阶段：" + value)
        return value

    @field_validator("problem_type")
    @classmethod
    def _problem_type(cls, value: str) -> str:
        if value not in PROBLEM_TYPE_TAGS:
            raise ValueError("未知问题类型：" + value)
        return value

    @model_validator(mode="after")
    def _role_stage_pair(self):
        if STAGE_BY_ROLE.get(self.role) != self.stage:
            raise ValueError("经验角色与阶段必须配对")
        return self


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    experiences: list[ExperienceDraft] = Field(default_factory=list, max_length=10)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    drafts: tuple[ExperienceDraft, ...]
    usage: dict = field(default_factory=dict)


class RetrospectiveExtractor(Protocol):
    async def extract(self, materials: dict) -> ExtractionResult: ...


def _fingerprint(draft: ExperienceDraft) -> str:
    payload = [
        draft.role,
        draft.stage,
        draft.problem_type,
        draft.applies_when,
        draft.recommendation,
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def normalize_drafts(
    drafts: Sequence[ExperienceDraft | dict],
    allowed_refs: set[str],
    *,
    limit: int = 10,
) -> list[dict]:
    """Validate and deduplicate one retrospective's output.

    Every experience must cite at least one process reference that actually
    exists in the materials; an empty result is valid when there is no basis.
    """

    normalized: list[dict] = []
    seen: set[str] = set()
    for item in drafts:
        draft = (
            item
            if isinstance(item, ExperienceDraft)
            else ExperienceDraft.model_validate(item)
        )
        unknown = [ref for ref in draft.evidence_refs if ref not in allowed_refs]
        if unknown:
            raise ExperienceExtractionError(
                "经验引用了不存在的过程依据：" + ", ".join(sorted(unknown)[:5])
            )
        fingerprint = _fingerprint(draft)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        normalized.append({**draft.model_dump(), "fingerprint": fingerprint})
        if len(normalized) >= limit:
            break
    return normalized


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

metadata = MetaData()

retrospective_jobs = Table(
    "retrospective_jobs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("report_id", String(128), nullable=False),
    Column("report_version", String(32), nullable=False),
    Column("report_artifact_id", String(256), nullable=False),
    Column("report_content_hash", String(64), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("status", String(16), nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False, default=3),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("locked_until", DateTime(timezone=True), nullable=True),
    Column("claim_token", String(64), nullable=True),
    Column("last_error", Text, nullable=True),
    Column("experience_count", Integer, nullable=False, default=0),
    Column("usage", JSON, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint(
        "report_id", "report_version", name="uq_retrospective_jobs_report_version"
    ),
)

experiences = Table(
    "experiences",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("source_report_id", String(128), nullable=False),
    Column("source_report_version", String(32), nullable=False),
    Column("source_job_id", String(64), nullable=False),
    Column("role", String(32), nullable=False),
    Column("stage", String(32), nullable=False),
    Column("problem_type", String(64), nullable=False),
    Column("applies_when", Text, nullable=False),
    Column("recommendation", Text, nullable=False),
    Column("evidence_refs", JSON, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("disabled_at", DateTime(timezone=True), nullable=True),
    Column("disabled_by", String(128), nullable=True),
    Column("fingerprint", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "source_report_id",
        "source_report_version",
        "fingerprint",
        name="uq_experiences_source_version_fingerprint",
    ),
)

experience_usages = Table(
    "experience_usages",
    metadata,
    Column("experience_id", String(64), primary_key=True),
    Column("report_id", String(128), primary_key=True),
    Column("dispatch_id", String(64), primary_key=True),
    Column("role", String(32), nullable=False),
    Column("stage", String(32), nullable=False),
    Column("recalled_at", DateTime(timezone=True), nullable=False),
    Column("adopted", Boolean, nullable=False, default=False),
    Column("adopted_at", DateTime(timezone=True), nullable=True),
    Column("adoption_refs", JSON, nullable=True),
)


def _insert_ignore(connection, table, values, index_elements):
    dialect = connection.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - production and offline tests cover both branches
        raise ValueError("Experience storage requires PostgreSQL or SQLite")
    statement = (
        dialect_insert(table)
        .values(values)
        .on_conflict_do_nothing(index_elements=list(index_elements))
    )
    return connection.execute(statement)


def _experience_dict(row) -> dict:
    return {
        "experience_id": row["id"],
        "role": row["role"],
        "stage": row["stage"],
        "problem_type": row["problem_type"],
        "applies_when": row["applies_when"],
        "recommendation": row["recommendation"],
        "evidence_refs": list(row["evidence_refs"] or []),
        "source_report_id": row["source_report_id"],
        "source_report_version": row["source_report_version"],
        "enabled": bool(row["enabled"]),
        "disabled_at": _iso(row["disabled_at"]),
        "disabled_by": row["disabled_by"],
        "created_at": _iso(row["created_at"]),
    }


def _job_dict(row) -> dict:
    return {
        "id": row["id"],
        "report_id": row["report_id"],
        "report_version": row["report_version"],
        "report_artifact_id": row["report_artifact_id"],
        "report_content_hash": row["report_content_hash"],
        "actor_id": row["actor_id"],
        "status": row["status"],
        "attempts": int(row["attempts"]),
        "max_attempts": int(row["max_attempts"]),
        "available_at": _iso(row["available_at"]),
        "locked_until": _iso(row["locked_until"]),
        "last_error": row["last_error"],
        "experience_count": int(row["experience_count"]),
        "usage": row["usage"] or {},
        "recovery_revision": (row["usage"] or {}).get("recovery_revision", 0),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
        "completed_at": _iso(row["completed_at"]),
    }


# ---------------------------------------------------------------------------
# Transaction effects (called inside the control-row transaction)
# ---------------------------------------------------------------------------


async def enqueue_delivery(
    connection,
    *,
    report_id: str,
    report_version: str,
    report_artifact_id: str,
    report_content_hash: str,
    actor_id: str,
    now: datetime | None = None,
) -> None:
    now = now or _lease_clock(connection)
    await _insert_ignore(
        connection,
        retrospective_jobs,
        {
            "id": uuid4().hex,
            "report_id": report_id,
            "report_version": report_version,
            "report_artifact_id": report_artifact_id,
            "report_content_hash": report_content_hash,
            "actor_id": actor_id,
            "status": "pending",
            "attempts": 0,
            "max_attempts": 3,
            "available_at": now,
            "locked_until": None,
            "claim_token": None,
            "last_error": None,
            "experience_count": 0,
            "usage": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        },
        ("report_id", "report_version"),
    )


async def record_recall(
    connection,
    *,
    report_id: str,
    dispatch_id: str,
    role: str,
    stage: str,
    experience_ids: Sequence[str],
    now: datetime | None = None,
) -> None:
    now = now or _utc_now()
    rows = [
        {
            "experience_id": ident,
            "report_id": report_id,
            "dispatch_id": dispatch_id,
            "role": role,
            "stage": stage,
            "recalled_at": now,
            "adopted": False,
            "adopted_at": None,
            "adoption_refs": None,
        }
        for ident in dict.fromkeys(experience_ids)
    ]
    if rows:
        await _insert_ignore(
            connection,
            experience_usages,
            rows,
            ("experience_id", "report_id", "dispatch_id"),
        )


async def record_adoption(
    connection,
    *,
    report_id: str,
    dispatch_id: str,
    experience_ids: Sequence[str],
    now: datetime | None = None,
    adoption_refs: dict | None = None,
) -> None:
    ids = list(dict.fromkeys(experience_ids))
    if not ids:
        return
    existing = (
        (
            await connection.execute(
                select(experience_usages.c.experience_id).where(
                    experience_usages.c.report_id == report_id,
                    experience_usages.c.dispatch_id == dispatch_id,
                    experience_usages.c.experience_id.in_(ids),
                )
            )
        )
        .scalars()
        .all()
    )
    missing = set(ids) - set(existing)
    if missing:
        raise ValueError(
            "任务结果引用了本次未召回的经验：" + ", ".join(sorted(missing)[:5])
        )
    await connection.execute(
        update(experience_usages)
        .where(
            experience_usages.c.report_id == report_id,
            experience_usages.c.dispatch_id == dispatch_id,
            experience_usages.c.experience_id.in_(ids),
        )
        .values(
            adopted=True,
            adopted_at=now or _utc_now(),
            adoption_refs=adoption_refs or {},
        )
    )


async def apply_effect(connection, context: RunContext, effect: dict) -> None:
    """Apply an experience side effect in the caller's open transaction."""

    kind = effect.get("kind")
    report_id = effect.get("report_id")
    if not report_id or report_id != context.report_id:
        raise PermissionError("Experience effect belongs to another report")
    if kind == "delivery_completed":
        artifact = ArtifactRef.model_validate(effect["artifact"])
        await enqueue_delivery(
            connection,
            report_id=report_id,
            report_version=artifact.version,
            report_artifact_id=artifact.artifact_id,
            report_content_hash=artifact.content_hash,
            actor_id=effect.get("actor_id") or context.actor_id,
        )
        return
    if kind == "experience_recall":
        await record_recall(
            connection,
            report_id=report_id,
            dispatch_id=effect["dispatch_id"],
            role=effect["role"],
            stage=effect["stage"],
            experience_ids=effect.get("experience_ids", ()),
        )
        return
    if kind == "experience_adopted":
        await record_adoption(
            connection,
            report_id=report_id,
            dispatch_id=effect["dispatch_id"],
            experience_ids=effect.get("experience_ids", ()),
            adoption_refs=effect.get("adoption_refs"),
        )
        return
    raise ValueError("Unknown experience effect: " + str(kind))


# ---------------------------------------------------------------------------
# Retrospective materials
# ---------------------------------------------------------------------------


def _artifact_dict(value) -> dict | None:
    if isinstance(value, ArtifactRef):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return None


def build_retrospective_materials(state, report_ref: ArtifactRef | dict) -> dict:
    """Build a bounded, deterministic view of the delivered work.

    Only the current delivery's review/rework records are included. The allowed
    reference list is the authority the extractor may cite; it is intentionally
    independent from free-form report text.
    """

    ref = _artifact_dict(report_ref)
    if not ref:
        raise ValueError("A delivered report reference is required")
    role_data = getattr(state, "role_data", {}) or {}
    allowed: set[str] = {f"report:{ref['version']}:{ref['content_hash'][:12]}"}
    reviews_out: list[dict] = []
    closures = 0
    issues_total = 0

    reviews = role_data.get("reviews", {}) or {}
    for review in reviews.values():
        if not isinstance(review, dict):
            continue
        review_id = str(review.get("review_id") or "")
        if not review_id:
            continue
        reviewed = _artifact_dict(review.get("reviewed")) or {}
        if reviewed:
            allowed.add(
                f"report:{reviewed.get('version')}:{str(reviewed.get('content_hash', ''))[:12]}"
            )
        allowed.add(f"review:{review_id}")
        issues = []
        for issue in review.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            issue_id = str(issue.get("issue_id") or "")
            if not issue_id:
                continue
            issues_total += 1
            allowed.add(f"review:{review_id}/issue:{issue_id}")
            issues.append(
                {
                    "issue_id": issue_id,
                    "category": _clip(issue.get("category"), 120),
                    "severity": _clip(issue.get("severity"), 32),
                    "target_id": _clip(issue.get("target_id"), 200),
                    "location": _clip(issue.get("location"), 300),
                    "quote": _clip(issue.get("quote"), 300),
                    "basis": _clip(issue.get("basis"), 800),
                    "reason": _clip(issue.get("reason"), 1200),
                    "suggestion": _clip(issue.get("suggestion"), 800),
                }
            )
        issue_closures = []
        for item in review.get("issue_closures") or []:
            if not isinstance(item, dict):
                continue
            issue_id = str(item.get("issue_id") or "")
            if not issue_id:
                continue
            closures += 1
            allowed.add(f"review:{review_id}/closure:{issue_id}")
            issue_closures.append(
                {
                    "issue_id": issue_id,
                    "target_id": _clip(item.get("target_id"), 200),
                    "quote": _clip(item.get("quote"), 300),
                    "reason": _clip(item.get("reason"), 800),
                }
            )
        feedback = review.get("feedback")
        comment = feedback.get("comment") if isinstance(feedback, dict) else ""
        reviews_out.append(
            {
                "review_id": review_id,
                "reviewed_version": reviewed.get("version"),
                "outcome": _clip(review.get("outcome"), 40),
                "summary": _clip(review.get("summary"), 800),
                "checks_complete": bool(review.get("checks_complete")),
                "pass_confidence": review.get("pass_confidence"),
                "comment": _clip(comment, 800),
                "issues": issues,
                "issue_closures": issue_closures,
            }
        )

    rework_out: list[dict] = []
    rework = role_data.get("rework", {}) or {}
    for artifact_id, payload in rework.items():
        if not isinstance(payload, dict):
            continue
        base_report = _artifact_dict(payload.get("base_report_ref")) or {}
        if base_report:
            allowed.add(
                f"report:{base_report.get('version')}:{str(base_report.get('content_hash', ''))[:12]}"
            )
        allowed.add(f"rework:{artifact_id}")
        tasks = []
        for index, task in enumerate(payload.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            allowed.add(f"rework:{artifact_id}/task:{index}")
            tasks.append(
                {
                    "role": _clip(task.get("role"), 32),
                    "objective": _clip(task.get("objective"), 1200),
                    "issue_ids": [
                        str(item) for item in task.get("issue_ids") or [] if item
                    ],
                }
            )
        rework_out.append(
            {
                "artifact_id": artifact_id,
                "review_id": _clip(payload.get("review_id"), 120),
                "base_report_version": base_report.get("version"),
                "tasks": tasks,
            }
        )

    results_out: list[dict] = []
    dispatches = getattr(state, "dispatches", {}) or {}
    for dispatch_id, result in (role_data.get("task_results", {}) or {}).items():
        if not isinstance(result, dict):
            continue
        allowed.add(f"task:{dispatch_id}")
        results_out.append(
            {
                "dispatch_id": dispatch_id,
                "role": _clip(dispatches.get(dispatch_id, {}).get("role"), 32),
                "status": _clip(result.get("status"), 32),
                "summary": _clip(result.get("summary"), 1200),
                "gaps": [_clip(item, 300) for item in result.get("gaps") or []],
                "used_experience_ids": [
                    str(item)
                    for item in result.get("used_experience_ids") or []
                    if item
                ],
            }
        )

    evidence_out: list[dict] = []
    for ident, item in list((role_data.get("evidence", {}) or {}).items())[:20]:
        if not isinstance(item, dict):
            continue
        allowed.add(f"evidence:{ident}")
        raw = item.get("raw")
        raw = raw if isinstance(raw, dict) else {}
        source = item.get("source")
        source = source if isinstance(source, dict) else {}
        evidence_out.append(
            {
                "evidence_id": ident,
                "source": _clip(source.get("server"), 80),
                "title": _clip(raw.get("title"), 200),
                "kind": _clip(item.get("kind"), 80),
            }
        )

    plan_out = {}
    artifacts = getattr(state, "artifacts", {}) or {}
    current_analysis = _artifact_dict(artifacts.get("analysis"))
    if current_analysis:
        allowed.add(
            f"analysis:{current_analysis['artifact_id']}:{current_analysis['version']}"
        )
    current_plan = _artifact_dict(artifacts.get("plan"))
    if current_plan:
        allowed.add(f"plan:{current_plan['artifact_id']}:{current_plan['version']}")
    plans = getattr(state, "plans", []) or []
    entries = [p for p in plans if p.get("artifact") == current_plan] or plans[-1:]
    if entries:
        plan = entries[0].get("plan") or {}
        plan_out = {
            "report_type": _clip(plan.get("report_type"), 80),
            "objective": _clip(plan.get("objective"), 2000),
            "tasks": [
                {
                    "task_id": _clip(task.get("task_id"), 120),
                    "role": _clip(task.get("role"), 32),
                    "objective": _clip(task.get("objective"), 800),
                    "problem_type": task.get("problem_type"),
                }
                for task in plan.get("tasks") or []
                if isinstance(task, dict)
            ],
        }

    return {
        "has_basis": bool(issues_total or closures or rework_out),
        "report": {
            "report_id": str(getattr(state, "role_data", {}).get("report_id", ""))
            or None,
            "version": ref["version"],
            "content_hash": ref["content_hash"],
            **plan_out,
        },
        "reviews": reviews_out,
        "rework": rework_out,
        "task_results": results_out,
        "evidence": evidence_out,
        "allowed_evidence_refs": sorted(allowed),
        "issue_count": issues_total,
        "closure_count": closures,
    }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class ExperienceStore:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self._claim_lock = asyncio.Lock()

    async def setup(self, connection) -> None:
        await connection.run_sync(metadata.create_all)

    async def recall(
        self,
        *,
        role: str,
        stage: str,
        problem_type: str | None = None,
        limit: int = 3,
    ) -> list[dict]:
        query = select(experiences).where(
            experiences.c.enabled.is_(True),
            experiences.c.role == role,
            experiences.c.stage == stage,
        )
        ordering: list[Any] = []
        if problem_type:
            ordering.append(
                case((experiences.c.problem_type == problem_type, 0), else_=1)
            )
        ordering.extend((experiences.c.created_at.desc(), experiences.c.id.desc()))
        query = query.order_by(*ordering).limit(max(1, min(limit, 10)))
        async with self.engine.connect() as connection:
            rows: Sequence[Any] = (await connection.execute(query)).mappings().all()
        return [_experience_dict(row) for row in rows]

    async def get(self, experience_id: str) -> dict | None:
        async with self.engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(experiences).where(experiences.c.id == experience_id)
                    )
                )
                .mappings()
                .first()
            )
        return _experience_dict(row) if row else None

    async def consumer_reports(self, experience_id: str) -> list[str]:
        async with self.engine.connect() as connection:
            rows: Sequence[Any] = (
                (
                    await connection.execute(
                        select(experience_usages.c.report_id)
                        .where(experience_usages.c.experience_id == experience_id)
                        .distinct()
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)

    async def set_enabled(
        self, experience_id: str, enabled: bool, *, actor_id: str
    ) -> dict | None:
        now = _utc_now()
        async with self.engine.begin() as connection:
            result = await connection.execute(
                update(experiences)
                .where(experiences.c.id == experience_id)
                .values(
                    enabled=enabled,
                    disabled_at=None if enabled else now,
                    disabled_by=None if enabled else actor_id,
                )
            )
            if result.rowcount != 1:
                return None
            row = (
                (
                    await connection.execute(
                        select(experiences).where(experiences.c.id == experience_id)
                    )
                )
                .mappings()
                .first()
            )
        return _experience_dict(row) if row else None

    async def jobs(self, report_id: str) -> list[dict]:
        async with self.engine.connect() as connection:
            rows: Sequence[Any] = (
                (
                    await connection.execute(
                        select(retrospective_jobs)
                        .where(retrospective_jobs.c.report_id == report_id)
                        .order_by(
                            retrospective_jobs.c.created_at.asc(),
                            retrospective_jobs.c.id.asc(),
                        )
                    )
                )
                .mappings()
                .all()
            )
        return [_job_dict(row) for row in rows]

    async def summary(self, report_id: str) -> dict:
        jobs = await self.jobs(report_id)
        produced = await self._produced_count(report_id)
        latest = jobs[-1] if jobs else None
        return {
            "status": latest["status"] if latest else "not_scheduled",
            "version": latest["report_version"] if latest else None,
            "attempts": latest["attempts"] if latest else 0,
            "experience_count": latest["experience_count"] if latest else 0,
            "last_error": latest["last_error"] if latest else None,
            "completed_at": latest["completed_at"] if latest else None,
            "produced_count": produced,
            "job_count": len(jobs),
        }

    async def _produced_count(self, report_id: str) -> int:
        async with self.engine.connect() as connection:
            count = (
                await connection.execute(
                    select(func.count())
                    .select_from(experiences)
                    .where(experiences.c.source_report_id == report_id)
                )
            ).scalar_one()
        return int(count)

    async def report_view(self, report_id: str) -> dict:
        jobs = await self.jobs(report_id)
        async with self.engine.connect() as connection:
            received_rows: Sequence[Any] = (
                (
                    await connection.execute(
                        select(
                            experiences,
                            experience_usages.c.dispatch_id.label("usage_dispatch_id"),
                            experience_usages.c.role.label("usage_role"),
                            experience_usages.c.stage.label("usage_stage"),
                            experience_usages.c.recalled_at.label("usage_recalled_at"),
                            experience_usages.c.adopted.label("usage_adopted"),
                            experience_usages.c.adopted_at.label("usage_adopted_at"),
                        )
                        .join(
                            experiences,
                            experiences.c.id == experience_usages.c.experience_id,
                        )
                        .where(experience_usages.c.report_id == report_id)
                        .order_by(
                            experience_usages.c.recalled_at.desc(),
                            experience_usages.c.experience_id.asc(),
                        )
                    )
                )
                .mappings()
                .all()
            )
            produced_rows: Sequence[Any] = (
                (
                    await connection.execute(
                        select(experiences)
                        .where(experiences.c.source_report_id == report_id)
                        .order_by(
                            experiences.c.created_at.desc(), experiences.c.id.asc()
                        )
                    )
                )
                .mappings()
                .all()
            )
            produced_ids = [row["id"] for row in produced_rows]
            usage_rows: Sequence[Any] = []
            if produced_ids:
                usage_rows = (
                    (
                        await connection.execute(
                            select(
                                experience_usages.c.experience_id,
                                func.count().label("recalled"),
                                func.sum(
                                    case(
                                        (experience_usages.c.adopted.is_(True), 1),
                                        else_=0,
                                    )
                                ).label("adopted"),
                            )
                            .where(experience_usages.c.experience_id.in_(produced_ids))
                            .group_by(experience_usages.c.experience_id)
                        )
                    )
                    .mappings()
                    .all()
                )
        usage_summary = {
            row["experience_id"]: {
                "recalled": int(row["recalled"]),
                "adopted": int(row["adopted"] or 0),
            }
            for row in usage_rows
        }
        received = []
        for row in received_rows:
            item = _experience_dict(row)
            item["usage"] = {
                "dispatch_id": row["usage_dispatch_id"],
                "role": row["usage_role"],
                "stage": row["usage_stage"],
                "recalled_at": _iso(row["usage_recalled_at"]),
                "adopted": bool(row["usage_adopted"]),
                "adopted_at": _iso(row["usage_adopted_at"]),
            }
            received.append(item)
        produced = []
        for row in produced_rows:
            item = _experience_dict(row)
            item["usage_summary"] = usage_summary.get(
                item["experience_id"], {"recalled": 0, "adopted": 0}
            )
            produced.append(item)
        return {
            "report_id": report_id,
            "jobs": jobs,
            "received": received,
            "produced": produced,
        }

    # -- worker-side operations ---------------------------------------------

    async def claim(self, *, lease_seconds: int = 300) -> dict | None:
        if lease_seconds <= 0:
            raise ValueError("Lease duration must be positive")
        async with self._claim_lock:
            async with self.engine.begin() as connection:
                now = _lease_clock(connection)
                query = (
                    select(retrospective_jobs)
                    .where(
                        or_(
                            and_(
                                retrospective_jobs.c.status == "pending",
                                retrospective_jobs.c.available_at <= now,
                            ),
                            and_(
                                retrospective_jobs.c.status == "running",
                                retrospective_jobs.c.locked_until.is_not(None),
                                retrospective_jobs.c.locked_until <= now,
                            ),
                        )
                    )
                    .order_by(
                        retrospective_jobs.c.created_at.asc(),
                        retrospective_jobs.c.id.asc(),
                    )
                    .limit(1)
                )
                if connection.dialect.name == "postgresql":
                    query = query.with_for_update(skip_locked=True)
                row = (await connection.execute(query)).mappings().first()
                if row is None:
                    return None
                unchanged = and_(
                    retrospective_jobs.c.id == row["id"],
                    retrospective_jobs.c.status == row["status"],
                    retrospective_jobs.c.claim_token == row["claim_token"],
                    retrospective_jobs.c.attempts == row["attempts"],
                    # Recheck eligibility after any lock wait or concurrent renewal.
                    or_(
                        retrospective_jobs.c.status == "pending",
                        retrospective_jobs.c.locked_until <= _lease_clock(connection),
                    ),
                )
                from research_copilot.runtime.retrospective_recovery import (
                    retry_is_authorized,
                )

                authorized = retry_is_authorized(row["usage"], int(row["attempts"]) + 1)
                uncertain = _recovery_required(row["usage"]) and not authorized
                if uncertain or int(row["attempts"]) >= int(row["max_attempts"]):
                    await connection.execute(
                        update(retrospective_jobs)
                        .where(unchanged)
                        .values(
                            status="failed",
                            last_error="recovery_required: 既有复盘请求用量或结果未知，禁止自动重发"
                            if uncertain
                            else "领取期限已过且尝试次数用尽",
                            locked_until=None,
                            claim_token=None,
                            updated_at=now,
                        )
                    )
                    return None
                token = uuid4().hex
                usage = row["usage"]
                if authorized:
                    usage = {
                        **usage,
                        "retry_authorization": {
                            **usage["retry_authorization"],
                            "consumed_by": token,
                        },
                    }
                result = await connection.execute(
                    update(retrospective_jobs)
                    .where(unchanged)
                    .values(
                        status="running",
                        attempts=int(row["attempts"]) + 1,
                        max_attempts=int(row["max_attempts"]),
                        locked_until=now + timedelta(seconds=lease_seconds),
                        claim_token=token,
                        usage=usage,
                        available_at=now,
                        updated_at=now,
                    )
                    .returning(*retrospective_jobs.c)
                )
                claimed = result.mappings().first()
                if claimed is None:
                    return None
                return {**_job_dict(claimed), "claim_token": token}

    async def renew(self, claim: dict, *, lease_seconds: int = 300) -> None:
        """Extend only an unexpired current owner; expired tokens cannot revive."""
        if lease_seconds <= 0:
            raise ValueError("Lease duration must be positive")
        async with self.engine.begin() as connection:
            now = _lease_clock(connection)
            result = await connection.execute(
                update(retrospective_jobs)
                .where(_live_claim(connection, claim))
                .values(
                    locked_until=now + timedelta(seconds=lease_seconds), updated_at=now
                )
            )
            if result.rowcount != 1:
                raise StaleClaim("复盘任务所有权已丢失或租约已过期")

    async def release(
        self,
        claim: dict,
        *,
        error: str | None = None,
        retry: bool = True,
        now: datetime | None = None,
    ) -> None:
        async with self.engine.begin() as connection:
            current = (
                (
                    await connection.execute(
                        select(retrospective_jobs)
                        .where(_live_claim(connection, claim))
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if current is None:
                return
            now = now or _lease_clock(connection)
            attempts = int(current["attempts"])
            uncertain = _recovery_required(current["usage"])
            will_retry = (
                retry and attempts < int(current["max_attempts"]) and not uncertain
            )
            values = {
                "status": "pending" if will_retry else "failed",
                "locked_until": None,
                "claim_token": None,
                "last_error": f"recovery_required: {error or '复盘请求结果未知'}"
                if uncertain
                else error,
                "updated_at": now,
                "available_at": now + timedelta(seconds=min(300, 30 * max(1, attempts)))
                if will_retry
                else now,
            }
            await connection.execute(
                update(retrospective_jobs)
                .where(_live_claim(connection, claim))
                .values(**values)
            )

    async def complete(
        self,
        claim: dict,
        drafts: Sequence[dict],
        *,
        usage: dict | None = None,
        now: datetime | None = None,
    ) -> int:
        async with self.engine.begin() as connection:
            now = now or _lease_clock(connection)
            current = (
                (
                    await connection.execute(
                        select(retrospective_jobs)
                        .where(_live_claim(connection, claim))
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if current is None:
                raise StaleClaim("复盘任务所有权已丢失或租约已过期")
            current_usage = current["usage"]
            for draft in drafts:
                await _insert_ignore(
                    connection,
                    experiences,
                    {
                        "id": uuid4().hex,
                        "source_report_id": claim["report_id"],
                        "source_report_version": claim["report_version"],
                        "source_job_id": claim["id"],
                        "role": draft["role"],
                        "stage": draft["stage"],
                        "problem_type": draft["problem_type"],
                        "applies_when": draft["applies_when"],
                        "recommendation": draft["recommendation"],
                        "evidence_refs": list(draft["evidence_refs"]),
                        "enabled": True,
                        "disabled_at": None,
                        "disabled_by": None,
                        "fingerprint": draft["fingerprint"],
                        "created_at": now,
                    },
                    ("source_report_id", "source_report_version", "fingerprint"),
                )
            result = await connection.execute(
                update(retrospective_jobs)
                .where(_live_claim(connection, claim))
                .values(
                    status="completed",
                    experience_count=len(drafts),
                    usage=current_usage
                    if current_usage and "attempts" in current_usage
                    else usage or {},
                    last_error=None,
                    locked_until=None,
                    claim_token=None,
                    updated_at=now,
                    completed_at=now,
                )
            )
            if result.rowcount != 1:
                raise StaleClaim("复盘任务领取已过期，结果已回滚")
        return len(drafts)


class StaleClaim(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Service used by dispatch and the API
# ---------------------------------------------------------------------------


class ExperienceService:
    def __init__(self, store):
        self.store = store

    @property
    def experience(self) -> ExperienceStore:
        experience = getattr(self.store, "experience", None)
        if experience is None:
            raise LookupError("经验存储未初始化")
        return experience

    async def recall(
        self, role: str, stage: str, problem_type: str | None = None
    ) -> list[dict]:
        return await self.experience.recall(
            role=role, stage=stage, problem_type=problem_type
        )

    async def report_view(self, actor_id: str, report_id: str) -> dict:
        record = await self.store.load(report_id, actor_id)
        if record.actor_id != actor_id:
            raise PermissionError("经验视图不属于当前操作者")
        return await self.experience.report_view(report_id)

    async def summary(self, report_id: str) -> dict:
        return await self.experience.summary(report_id)

    async def recover(self, actor_id, report_id, job_id, request):
        from research_copilot.runtime.retrospective_recovery import recover_job

        await self.store.load(report_id, actor_id)
        return await recover_job(
            self.store.engine, actor_id, report_id, job_id, request
        )

    async def set_enabled(
        self, actor_id: str, experience_id: str, enabled: bool
    ) -> dict:
        record = await self.experience.get(experience_id)
        if record is None:
            raise LookupError("经验不存在")
        allowed = False
        try:
            source = await self.store.load(record["source_report_id"], actor_id)
            allowed = source.actor_id == actor_id
        except (LookupError, PermissionError):
            allowed = False
        if not allowed:
            for report_id in await self.experience.consumer_reports(experience_id):
                try:
                    consumer = await self.store.load(report_id, actor_id)
                except (LookupError, PermissionError):
                    continue
                if consumer.actor_id == actor_id:
                    allowed = True
                    break
        if not allowed:
            raise PermissionError("只能停用本操作者报告产生或收到的经验")
        updated = await self.experience.set_enabled(
            experience_id, enabled, actor_id=actor_id
        )
        if updated is None:
            raise LookupError("经验不存在")
        return updated


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class RetrospectiveWorker:
    """One background worker per web service; jobs survive process restarts."""

    def __init__(
        self,
        store,
        extractor: RetrospectiveExtractor,
        *,
        poll_interval: float = 2.0,
        lease_seconds: int = 300,
    ):
        self.store = store
        self.experience = store.experience
        self.extractor = extractor
        self.poll_interval = max(0.05, float(poll_interval))
        self.lease_seconds = max(1, int(lease_seconds))
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._stop_lock = asyncio.Lock()
        watch = getattr(store, "runtime_watch", None)
        if watch is not None:
            watch.subscribe(self.stop)

    async def start(self) -> None:
        from research_copilot.runtime.runtime_watch import require_live

        require_live(self.store)
        if self._task is not None:
            raise RuntimeError("复盘 worker 已启动")
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="retrospective-worker")

    async def stop(self) -> None:
        async with self._stop_lock:
            await self._stop_once()

    async def _stop_once(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - run_once records job errors
                logger.exception("复盘 worker 循环异常")
                worked = False
            if not worked:
                await asyncio.sleep(self.poll_interval)

    async def run_once(self) -> bool:
        from research_copilot.runtime.runtime_watch import require_live

        require_live(self.store)
        claim = await self.experience.claim(lease_seconds=self.lease_seconds)
        if claim is None:
            return False
        work = asyncio.create_task(
            self._run_claim(claim), name="retrospective-extraction"
        )

        async def heartbeat():
            interval = self.lease_seconds / 3
            while True:
                await asyncio.sleep(interval)
                async with asyncio.timeout(interval):
                    await self.experience.renew(claim, lease_seconds=self.lease_seconds)

        renewal = asyncio.create_task(heartbeat(), name="retrospective-lease")
        try:
            done, _ = await asyncio.wait(
                {work, renewal}, return_when=asyncio.FIRST_COMPLETED
            )
            if work in done:
                return await work
            # Lost ownership or renewal availability: stop local work. A provider
            # may still finish remotely; its unknown attempt is never auto-retried.
            try:
                await renewal
            except StaleClaim:
                return True
        finally:
            work.cancel()
            renewal.cancel()
            await asyncio.gather(work, renewal, return_exceptions=True)
        return True

    async def _run_claim(self, claim) -> bool:
        attempt_started = False
        from research_copilot.runtime.retrospective_usage import record_attempt

        async def audit_usage(usage):
            await record_attempt(
                self.store.engine, claim, status="response_received", usage=usage
            )

        try:
            record = await self.store.load(claim["report_id"], claim["actor_id"])
            context = RunContext(
                record.actor_id,
                record.report_id,
                record.run_id,
                record.budget_id,
                record.policy_version,
            )
            state = await self.store.read_control(context)
            report_ref = ArtifactRef(
                artifact_id=claim["report_artifact_id"],
                version=claim["report_version"],
                content_hash=claim["report_content_hash"],
            )
            if (
                state.artifacts.get("report") != report_ref
                or state.approved.get("report") != report_ref
            ):
                await self.experience.release(
                    claim, error="交付版本已不再是当前已签署版本", retry=False
                )
                return True
            materials = build_retrospective_materials(state, report_ref)
            materials["report"]["report_id"] = claim["report_id"]
            if not materials["has_basis"]:
                await self.experience.complete(claim, [], usage={})
                return True
            await record_attempt(self.store.engine, claim, status="started", usage={})
            attempt_started = True
            from research_copilot.runtime.runtime_watch import require_live

            require_live(self.store)
            if isinstance(self.extractor, LLMExperienceExtractor):
                result = await self.extractor.extract(materials, on_usage=audit_usage)
            else:
                result = await self.extractor.extract(materials)
            if isinstance(result, ExtractionResult):
                drafts, usage = result.drafts, result.usage
            else:  # tolerate simple test extractors returning lists
                drafts, usage = result, {}
            normalized = normalize_drafts(
                drafts, set(materials["allowed_evidence_refs"])
            )
            await record_attempt(
                self.store.engine, claim, status="completed", usage=usage
            )
            await self.experience.complete(claim, normalized, usage=usage)
            return True
        except asyncio.CancelledError:
            try:
                if attempt_started:
                    await asyncio.shield(
                        record_attempt(self.store.engine, claim, status="interrupted")
                    )
                await asyncio.shield(
                    self.experience.release(claim, error="worker_stopped", retry=True)
                )
            except Exception:  # pragma: no cover - loop teardown
                logger.exception("停止复盘 worker 时释放任务失败")
            raise
        except StaleClaim:
            # Another worker completed the job after our lease was replaced.
            return True
        except LookupError as exc:
            if attempt_started:
                await record_attempt(self.store.engine, claim, status="failed")
            await self.experience.release(claim, error=str(exc), retry=False)
            return True
        except Exception as exc:
            logger.exception("复盘任务处理失败")
            if attempt_started:
                await record_attempt(self.store.engine, claim, status="failed")
            await self.experience.release(
                claim, error=f"{type(exc).__name__}: {exc}", retry=True
            )
            return True


# ---------------------------------------------------------------------------
# LLM extractor
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM_PROMPT = """你是研究复盘提炼器。只输出一个 JSON 对象，不要 Markdown、代码围栏或解释。

输出格式：
{"experiences":[{"role":"searcher|analysist|writer","stage":"collect|analysis|writing","problem_type":"<问题类型>","applies_when":"<什么情况下>","recommendation":"<建议怎么做>","evidence_refs":["<allowed_evidence_refs 中的一项或多项>"]}]}

规则：
1. 只提炼可跨标的复用的方法、检查步骤或流程改进；不得写具体公司、标的、价格、目标价、估值结论或用户偏好。
2. 每条经验必须同时说明：什么情况下适用、建议怎么做、哪段交付过程支持这个判断。
3. evidence_refs 必须逐字来自输入中的 allowed_evidence_refs，且至少一项。
4. 没有可靠依据时返回 {"experiences":[]}；宁缺毋滥，最多 10 条。
5. 经验应能指导后续同类研究，不要复述本次报告正文。
问题类型只能取：__PROBLEM_TYPES__
角色与阶段必须配对：searcher→collect，analysist→analysis，writer→writing。
"""


class LLMExperienceExtractor:
    """One bounded JSON call per job; usage is recorded on the job row."""

    def __init__(self, model, *, max_output_tokens: int | None = None):
        self.model = model
        profile = getattr(model, "profile", None) or {}
        self.max_output_tokens = int(
            max_output_tokens or profile.get("max_output_tokens") or 2048
        )

    async def extract(self, materials: dict, *, on_usage=None) -> ExtractionResult:
        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = _EXTRACTION_SYSTEM_PROMPT.replace(
            "__PROBLEM_TYPES__", ", ".join(PROBLEM_TYPE_TAGS)
        )
        payload = json.dumps(materials, ensure_ascii=False, sort_keys=True)
        response = await self.model.ainvoke(
            [
                SystemMessage(content=prompt),
                HumanMessage(content=payload),
            ],
            max_tokens=self.max_output_tokens,
        )
        usage = dict(getattr(response, "usage_metadata", None) or {})
        usage.setdefault("model", getattr(self.model, "model_name", None))
        usage["provider_request_id"] = (
            getattr(response, "response_metadata", None) or {}
        ).get("request_id")
        usage["provider_response_id"] = getattr(response, "id", None)
        if on_usage:
            await on_usage(usage)
        raw = getattr(response, "content", "")
        if not isinstance(raw, str) or not raw.strip():
            raise ExperienceExtractionError("复盘模型返回空内容")
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ExperienceExtractionError("复盘模型未返回 JSON 对象")
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ExperienceExtractionError("复盘模型 JSON 无法解析") from exc
        output = ExtractionOutput.model_validate(value)
        return ExtractionResult(drafts=tuple(output.experiences), usage=usage)
