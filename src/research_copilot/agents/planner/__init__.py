"""Planner Agent 包。

阶段二逐步加入契约、提示词、严格校验、模型调用边界和最小 LangGraph 集成。
当前导出契约、错误类型、提示词、业务校验和 Planner 执行入口；
LangGraph 单节点编排位于 `research_copilot.orchestration.planner`。
"""

from research_copilot.agents.planner.contracts import (
    AttachmentMetadata,
    Capability,
    DataRequirement,
    MarketScope,
    MaterialType,
    PlanStep,
    PlannerInput,
    PlannerOutput,
    PlanningStatus,
    ResearchObjectType,
    ResearchObjective,
    ResearchScope,
    UnresolvedReference,
    UnresolvedReferenceKind,
    export_planner_schemas,
)
from research_copilot.agents.planner.errors import (
    PlannerError,
    PlannerErrorCode,
    PlannerModelError,
    PlannerOutputError,
)
from research_copilot.agents.planner.prompts import (
    PLANNER_PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_planner_prompt,
    build_planner_user_prompt,
)
from research_copilot.agents.planner.validation import validate_planner_output
from research_copilot.agents.planner.agent import Planner

__all__ = [
    "AttachmentMetadata",
    "Capability",
    "DataRequirement",
    "MarketScope",
    "MaterialType",
    "PLANNER_PROMPT_VERSION",
    "PlanStep",
    "Planner",
    "PlannerError",
    "PlannerErrorCode",
    "PlannerInput",
    "PlannerModelError",
    "PlannerOutput",
    "PlannerOutputError",
    "PlanningStatus",
    "ResearchObjectType",
    "ResearchObjective",
    "ResearchScope",
    "SYSTEM_PROMPT",
    "UnresolvedReference",
    "UnresolvedReferenceKind",
    "build_planner_prompt",
    "build_planner_user_prompt",
    "export_planner_schemas",
    "validate_planner_output",
]
