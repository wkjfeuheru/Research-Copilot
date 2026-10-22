"""Planner 输出的业务校验。

Pydantic 负责字段类型和结构；本模块负责依赖关系与状态语义，例如步骤引用、
数据需求引用、技能来源、能力覆盖和状态特定内容。校验失败统一抛出带错误码的
``PlannerOutputError``，调用方不得继续把输出写入共享状态。
"""

from collections import Counter
from collections.abc import Sequence

from research_copilot.agents.planner.contracts import (
    Capability,
    PlannerInput,
    PlannerOutput,
    PlanStep,
    PlanningStatus,
)
from research_copilot.agents.planner.errors import PlannerErrorCode, PlannerOutputError

_REQUIRED_READY_CAPABILITIES: tuple[Capability, ...] = (
    Capability.EVIDENCE_ACQUISITION,
    Capability.ANALYSIS,
    Capability.RISK_CHECK,
    Capability.REPORTING,
)


def validate_planner_output(
    request: PlannerInput,
    output: PlannerOutput,
) -> None:
    """校验 Planner 输出与输入、步骤依赖和状态语义的一致性。

    通过时返回 ``None``；失败时抛出 ``PlannerOutputError``。
    """

    step_ids = _collect_unique_ids(
        [step.step_id for step in output.steps],
        "step_id",
    )
    requirement_ids = _collect_unique_ids(
        [requirement.requirement_id for requirement in output.data_requirements],
        "requirement_id",
    )
    known_steps = set(step_ids)
    known_requirements = set(requirement_ids)
    known_skills = set(request.selected_skill_ids)
    referenced_requirements: set[str] = set()

    for step in output.steps:
        _validate_step_references(
            step,
            known_steps=known_steps,
            known_requirements=known_requirements,
            known_skills=known_skills,
            referenced_requirements=referenced_requirements,
        )

    _reject_cycles(output.steps)
    _validate_status_contract(
        output,
        requirement_ids=requirement_ids,
        referenced_requirements=referenced_requirements,
    )


def _collect_unique_ids(values: Sequence[str], field_name: str) -> list[str]:
    duplicates = sorted(
        value
        for value, count in Counter(values).items()
        if count > 1
    )
    if duplicates:
        raise PlannerOutputError(
            PlannerErrorCode.DUPLICATE_IDENTIFIER,
            f"{field_name} 重复：{', '.join(duplicates)}",
        )
    return list(values)


def _validate_step_references(
    step: PlanStep,
    *,
    known_steps: set[str],
    known_requirements: set[str],
    known_skills: set[str],
    referenced_requirements: set[str],
) -> None:
    if step.step_id in step.depends_on:
        raise PlannerOutputError(
            PlannerErrorCode.INVALID_STEP_REFERENCE,
            f"步骤 {step.step_id} 不能依赖自身",
        )

    unknown_steps = sorted(set(step.depends_on) - known_steps)
    if unknown_steps:
        raise PlannerOutputError(
            PlannerErrorCode.INVALID_STEP_REFERENCE,
            f"步骤 {step.step_id} 引用了不存在的步骤：{', '.join(unknown_steps)}",
        )

    unknown_requirements = sorted(
        set(step.data_requirement_ids) - known_requirements
    )
    if unknown_requirements:
        raise PlannerOutputError(
            PlannerErrorCode.INVALID_DATA_REFERENCE,
            f"步骤 {step.step_id} 引用了不存在的数据需求："
            f"{', '.join(unknown_requirements)}",
        )

    unknown_skills = sorted(set(step.skill_ids) - known_skills)
    if unknown_skills:
        raise PlannerOutputError(
            PlannerErrorCode.UNKNOWN_SKILL,
            f"步骤 {step.step_id} 使用了输入未提供的技能："
            f"{', '.join(unknown_skills)}",
        )

    referenced_requirements.update(step.data_requirement_ids)


def _reject_cycles(steps: Sequence[PlanStep]) -> None:
    graph = {step.step_id: list(dict.fromkeys(step.depends_on)) for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> bool:
        if step_id in visiting:
            return True
        if step_id in visited:
            return False
        visiting.add(step_id)
        for dependency in graph.get(step_id, ()):
            if visit(dependency):
                return True
        visiting.remove(step_id)
        visited.add(step_id)
        return False

    for step_id in graph:
        if visit(step_id):
            raise PlannerOutputError(
                PlannerErrorCode.INVALID_STEP_DEPENDENCY,
                f"步骤依赖存在循环，涉及步骤：{step_id}",
            )


def _validate_status_contract(
    output: PlannerOutput,
    *,
    requirement_ids: Sequence[str],
    referenced_requirements: set[str],
) -> None:
    if output.status is PlanningStatus.READY:
        _validate_ready(output, requirement_ids, referenced_requirements)
    elif output.status is PlanningStatus.NEEDS_CLARIFICATION:
        _validate_needs_clarification(output)
    elif output.status is PlanningStatus.UNSUPPORTED:
        _validate_unsupported(output)


def _validate_ready(
    output: PlannerOutput,
    requirement_ids: Sequence[str],
    referenced_requirements: set[str],
) -> None:
    if output.objective is None:
        _missing("ready 计划必须包含 objective")
    if not output.objective.analysis_dimensions:
        _missing("ready 计划必须包含至少一个分析维度")
    if not output.data_requirements:
        _missing("ready 计划必须包含至少一个数据需求")
    if not output.steps:
        _missing("ready 计划必须包含至少一个执行步骤")
    if output.clarification_questions:
        _invalid_status("ready 计划不能包含 clarification_questions")
    if output.unsupported_reason is not None:
        _invalid_status("ready 计划不能包含 unsupported_reason")

    covered = {
        capability
        for step in output.steps
        for capability in step.capabilities
    }
    missing_capabilities = [
        capability.value
        for capability in _REQUIRED_READY_CAPABILITIES
        if capability not in covered
    ]
    if missing_capabilities:
        _missing(f"ready 计划缺少能力：{', '.join(missing_capabilities)}")

    unreferenced = [
        requirement_id
        for requirement_id in requirement_ids
        if requirement_id not in referenced_requirements
    ]
    if unreferenced:
        _missing(
            "以下数据需求没有被任何步骤引用："
            + ", ".join(unreferenced)
        )


def _validate_needs_clarification(output: PlannerOutput) -> None:
    if not output.clarification_questions:
        _missing("needs_clarification 至少需要一个待澄清问题")
    if output.unsupported_reason is not None:
        _invalid_status(
            "needs_clarification 不能包含 unsupported_reason"
        )


def _validate_unsupported(output: PlannerOutput) -> None:
    if output.unsupported_reason is None:
        _missing("unsupported 必须包含 unsupported_reason")
    if output.steps:
        _invalid_status("unsupported 不能包含执行步骤")
    if output.data_requirements:
        _invalid_status("unsupported 不能包含数据需求")
    if output.clarification_questions:
        _invalid_status("unsupported 不能包含待澄清问题")


def _missing(message: str) -> None:
    raise PlannerOutputError(PlannerErrorCode.REQUIRED_CONTENT_MISSING, message)


def _invalid_status(message: str) -> None:
    raise PlannerOutputError(PlannerErrorCode.INVALID_STATUS_CONTENT, message)


__all__ = ["validate_planner_output"]
