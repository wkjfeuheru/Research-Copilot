"""Planner 测试夹具。

这些数据只用于固定模型响应下的程序验证，不代表真实模型规划质量。
"""

from copy import deepcopy
from typing import Any

from research_copilot.agents.planner.contracts import PlannerInput, PlannerOutput

SELECTED_SKILL_IDS = ["financial-analysis"]


def planner_request(**overrides: Any) -> PlannerInput:
    payload: dict[str, Any] = {
        "research_question": "分析贵州茅台最近三个年度的利润增长质量与现金流匹配情况。",
        "cutoff_date": "2025-12-31",
        "selected_skill_ids": list(SELECTED_SKILL_IDS),
        "attachments": [
            {
                "attachment_id": "attachment-1",
                "filename": "annual-report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 4096,
            }
        ],
        "user_notes": "重点关注经营性现金流。",
    }
    payload.update(overrides)
    return PlannerInput.model_validate(payload)


def ready_plan_payload() -> dict[str, Any]:
    return deepcopy(
        {
            "status": "ready",
            "research_scope": {
                "object_type": "stock",
                "subject": "贵州茅台",
                "unresolved_references": [],
            },
            "objective": {
                "goal": "判断利润增长是否由可持续经营驱动。",
                "analysis_dimensions": ["收入增长", "利润率", "经营现金流"],
                "assumptions": ["以最近完整会计年度为主要观察期。"],
            },
            "data_requirements": [
                {
                    "requirement_id": "D1",
                    "description": "获取最近三个年度的利润表和现金流量表。",
                    "material_types": ["financial_statement"],
                    "metrics": ["营业收入", "净利润", "经营现金流"],
                    "period": "最近三个完整会计年度",
                    "purpose": "核对利润增长与现金流是否匹配。",
                }
            ],
            "steps": [
                {
                    "step_id": "S1",
                    "intent": "获取并核对财务数据。",
                    "capabilities": ["evidence_acquisition"],
                    "depends_on": [],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": ["financial-analysis"],
                },
                {
                    "step_id": "S2",
                    "intent": "分析增长质量与现金流匹配。",
                    "capabilities": ["analysis"],
                    "depends_on": ["S1"],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": ["financial-analysis"],
                },
                {
                    "step_id": "S3",
                    "intent": "检查证据缺口与反面证据。",
                    "capabilities": ["risk_check"],
                    "depends_on": ["S2"],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": [],
                },
                {
                    "step_id": "S4",
                    "intent": "形成带引用的简报结构。",
                    "capabilities": ["reporting"],
                    "depends_on": ["S3"],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": [],
                },
            ],
            "clarification_questions": [],
        }
    )


def ready_plan_output() -> PlannerOutput:
    return PlannerOutput.model_validate(ready_plan_payload())


def industry_plan_payload() -> dict[str, Any]:
    return deepcopy(
        {
            "status": "ready",
            "research_scope": {
                "object_type": "industry",
                "subject": "新能源设备行业",
                "unresolved_references": [],
            },
            "objective": {
                "goal": "判断行业景气度、增长驱动与竞争格局。",
                "analysis_dimensions": ["需求增长", "供给格局", "政策影响"],
                "assumptions": ["以公开行业资料为主要来源。"],
            },
            "data_requirements": [
                {
                    "requirement_id": "D1",
                    "description": "获取行业规模、装机量和主要公司份额。",
                    "material_types": ["industry_report", "market_data"],
                    "metrics": ["市场规模", "装机量", "市场份额"],
                    "period": "2023-2026",
                    "purpose": "判断行业增长与竞争结构。",
                }
            ],
            "steps": [
                {
                    "step_id": "S1",
                    "intent": "收集行业报告与市场数据。",
                    "capabilities": ["evidence_acquisition"],
                    "depends_on": [],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": ["industry-comparison"],
                },
                {
                    "step_id": "S2",
                    "intent": "分析需求、供给与竞争格局。",
                    "capabilities": ["analysis"],
                    "depends_on": ["S1"],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": ["industry-comparison"],
                },
                {
                    "step_id": "S3",
                    "intent": "检查证据缺口与行业风险。",
                    "capabilities": ["risk_check"],
                    "depends_on": ["S2"],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": [],
                },
                {
                    "step_id": "S4",
                    "intent": "形成行业研究简报结构。",
                    "capabilities": ["reporting"],
                    "depends_on": ["S3"],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": [],
                },
            ],
            "clarification_questions": [],
        }
    )


def company_comparison_plan_payload() -> dict[str, Any]:
    return deepcopy(
        {
            "status": "ready",
            "research_scope": {
                "object_type": "company_comparison",
                "subject": "三家新能源设备公司",
                "unresolved_references": [],
            },
            "objective": {
                "goal": "比较三家公司的经营质量、现金流与风险。",
                "analysis_dimensions": ["增长", "利润率", "现金流", "风险"],
                "assumptions": ["统一使用同一报告期和财务口径。"],
            },
            "data_requirements": [
                {
                    "requirement_id": "D1",
                    "description": "获取三家公司同一报告期的财务数据。",
                    "material_types": ["financial_statement"],
                    "metrics": ["营业收入", "净利润", "经营现金流"],
                    "period": "最近三个完整会计年度",
                    "purpose": "进行同口径比较。",
                }
            ],
            "steps": [
                {
                    "step_id": "S1",
                    "intent": "收集三家公司的财务数据。",
                    "capabilities": ["evidence_acquisition"],
                    "depends_on": [],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": ["industry-comparison"],
                },
                {
                    "step_id": "S2",
                    "intent": "按统一口径比较经营表现。",
                    "capabilities": ["analysis"],
                    "depends_on": ["S1"],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": ["industry-comparison"],
                },
                {
                    "step_id": "S3",
                    "intent": "检查差异来源与证据缺口。",
                    "capabilities": ["risk_check"],
                    "depends_on": ["S2"],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": [],
                },
                {
                    "step_id": "S4",
                    "intent": "形成公司对比简报结构。",
                    "capabilities": ["reporting"],
                    "depends_on": ["S3"],
                    "data_requirement_ids": ["D1"],
                    "skill_ids": [],
                },
            ],
            "clarification_questions": [],
        }
    )


def needs_clarification_payload() -> dict[str, Any]:
    return deepcopy(
        {
            "status": "needs_clarification",
            "research_scope": {
                "object_type": "stock",
                "subject": "待确认的公司",
            },
            "clarification_questions": ["请确认具体公司名称和股票代码。"],
        }
    )


def unsupported_payload() -> dict[str, Any]:
    return deepcopy(
        {
            "status": "unsupported",
            "research_scope": {
                "object_type": "stock",
                "subject": "海外市场标的",
            },
            "unsupported_reason": "当前只支持 A 股个股、行业和公司对比。",
        }
    )
