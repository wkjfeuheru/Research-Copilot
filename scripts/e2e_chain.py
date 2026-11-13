"""Research Copilot full-chain real-model E2E test driver.

This driver runs the *actual* supervisor/subagent/reviewer graph with real
DeepSeek and FinanceKB calls, persistent PostgreSQL checkpoints, the Analysist
Docker sandbox, and explicit operator approvals. It records every model/source
call, dispatch, artifact, approval, review and execution under the output dir.

It intentionally does not reuse the V1 ``scripts/e2e_live_test.py`` pipeline:
that pipeline is a hand-written five-stage chain and does not exercise
orchestration, role isolation, sandbox receipts, or independent review.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


from research_copilot.runtime.host_settings import HostSettings
from research_copilot.runtime.report_rules import load_report_rules
from research_copilot.contracts import ArtifactRef, RunContext
from research_copilot.llm.config import ModelsConfig, load_models_config, open_models
from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.identity import RUNTIME_VERSION
from research_copilot.runtime.business import BusinessCoordinator
from research_copilot.runtime.session import SessionRecord, SessionService
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.sources.financekb import open_financekb_sources
from research_copilot.subagents.services import RoleServices, digest
from research_copilot.workspace import bundled_resources
from research_copilot.workspace.inputs import WorkspaceFiles
from scripts.acceptance_budget import CallBudget

# Frozen acceptance identities and input paths belong to existing runs, not code naming.
REPORT_ID = "v2-e2e-20261030-wuliangye-r5"
ACTOR_ID = "local-v2-e2e-operator"
POLICY_VERSION = "v2-e2e-1"
TEST_POLICY = PROJECT_ROOT / "config" / "e2e_compliance_rules.yaml"
ATTACHMENT = PROJECT_ROOT / "data" / "e2e_v2_inputs" / "wuliangye_2024_facts.txt"

QUESTION = """请以五粮液(000858.SZ)2024年年度报告（报告期2024-01-01至2024-12-31）及可核实的公开披露为依据，撰写一份2024年报业绩点评（report_type=flash_comment），核心判断是“营收与归母净利润双增，但增长质量与费用投放是否匹配”。

请完成：
1. 提取并核实2024年营业总收入、归母净利润、毛利率、归母净利率、销售费用、销售费用率，以及2023年对应可比数据；
2. 直接采用证据中披露的同比增速与百分点变动，分析增长质量；沙盒一次完成必要计算与登记，明确失败后最多修复一次，未知状态不得自动重跑；
3. 结合白酒行业景气与高端酒竞争格局，分析增长来源、费用投放效果和盈利质量；
4. 给出简明的投资判断与风险提示。

约束：
- 以2024年年报为准，不采用2025年或2026年数据；as_of=2025-04-30。
- 已附带《五粮液2024年报与行业数据摘录》作为受控证据包；请优先登记、核验并引用该附件，最多进行1次真实网页检索用于交叉核验行业数据。若检索失败，明确记录缺口，不得编造，也不得因此阻塞报告。
- required_fields 最多11个，仅限：revenue_2024、revenue_2023、net_profit_attr_2024、net_profit_attr_2023、gross_margin_2024、gross_margin_2023、net_margin_attr_2024、net_margin_attr_2023、selling_expense_2024、selling_expense_ratio_2024、selling_expense_ratio_2023。不要再要求selling_expense_2023金额；用销售费用率对比替代。
- optional_fields 最多3个，仅限：industry_baijiu_revenue_yoy、net_profit_attr_q4_2024_yoy、p5_wholesale_price_end_2024。不要再采集合同负债、分产品收入、营业成本等其他字段。
- facts 最多15条；searcher 的 summary 不超过800字符；coverage 不超过8条；source_warnings 不超过5条；不复制长证据正文。
- 不要求毛利率恒等式复算和营业成本；不要求图表、目标价或评级；估值数据不足直接说明。
- 所有数字必须来自已登记证据或验证后的指标，并带 [^E-XXXXXX] 引用；缺失数据明确说明。写作提交绑定正文哈希的 claims_path。
- 分析将增长剪刀差1.65个百分点作为 required_in_report 核心结论；率差只描述毛利率与销售费用率的变动之差，删除错误的净利率贡献、税后影响和残差归因。
- 推算2023销售费用及衍生值必须传播推算属性；费用增速/收入增速显示5.24倍；普五批价相对出厂价倒挂幅度=(1019-批价)/1019，批价910—920元/瓶，显示推算9.72%—10.70%，不是销售费用率敏感性区间。摘要写明毛利率由75.79%升至77.05%，提升1.26个百分点。
- 每个派发提供合法 TaskBrief，冻结 as_of；搜索任务包含 entity_terms（五粮液、000858或白酒行业）及 topic_terms（年报、业绩、行业收入等）。超过基准日、日期未知或弱相关网页不能作独立佐证。
- 原r4审核问题须逐项提供关闭证据；条件式讨论费用投入与增长质量，说明证据限制及替代解释。受控包历史可得性仍为 unknown。
- 报告须有标题、摘要（结论前的一段摘要）、正文分析、结论/风险提示，按 flash_comment 模板结构。"""


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class StageDecision:
    interrupt_id: str
    kind: str
    approved: bool
    rationale: str
    payload: dict[str, Any]


class ModelCallLog:
    """Wrap an OpenAI-compatible client and persist each real model call."""

    def __init__(
        self,
        root: Path,
        calls: CallBudget,
        existing: list[dict[str, Any]] | None = None,
    ):
        self.root, self.calls = root, calls
        self.records: list[dict[str, Any]] = list(existing or [])

    def wrap(self, role: str, model) -> None:
        original = model.client.chat.completions.create

        async def create(**kwargs):
            self.calls.reserve("model")
            sequence = self.calls.state["used"]["model"]
            started_at = now_iso()
            started = time.monotonic()
            request_path = (
                self.root / "model_calls" / f"{sequence:03}-{role}-request.json"
            )
            response_path = (
                self.root / "model_calls" / f"{sequence:03}-{role}-response.json"
            )
            save_json(
                request_path,
                {
                    "sequence": sequence,
                    "role": role,
                    "model": getattr(model, "model_name", ""),
                    "started_at": started_at,
                    "request": kwargs,
                },
            )
            record = {
                "sequence": sequence,
                "role": role,
                "model": getattr(model, "model_name", ""),
                "started_at": started_at,
                "status": "running",
                "input_chars": len(
                    json.dumps(kwargs.get("messages", []), ensure_ascii=False)
                ),
                "request_file": str(request_path.relative_to(self.root)),
            }
            try:
                result = await original(**kwargs)
            except Exception as exc:
                record.update(
                    status="error",
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    error=f"{type(exc).__name__}: {exc}"[:1200],
                )
                self.records.append(record)
                save_json(self.root / "model_calls.json", self.records)
                save_json(
                    response_path,
                    {
                        "sequence": sequence,
                        "role": role,
                        "status": "error",
                        "error": record["error"],
                    },
                )
                raise
            elapsed = time.monotonic() - started
            message = result.choices[0].message
            usage = result.usage.total_tokens if result.usage else None
            record.update(
                status="success",
                elapsed_seconds=round(elapsed, 3),
                usage_tokens=usage,
                finish_reason=result.choices[0].finish_reason,
                output_chars=len(message.content or ""),
                tool_calls=len(message.tool_calls or []),
                response_file=str(response_path.relative_to(self.root)),
            )
            self.records.append(record)
            save_json(self.root / "model_calls.json", self.records)
            save_json(
                response_path,
                {
                    "sequence": sequence,
                    "role": role,
                    "status": "success",
                    "elapsed_seconds": round(elapsed, 3),
                    "response": result.model_dump(),
                },
            )
            return result

        model.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )


class SourceCallLog:
    """Wrap approved FinanceKB tools and persist every source call."""

    def __init__(
        self,
        root: Path,
        calls: CallBudget,
        existing: list[dict[str, Any]] | None = None,
    ):
        self.root, self.calls = root, calls
        self.records: list[dict[str, Any]] = list(existing or [])

    def wrap(self, source):
        original = source.fetch

        async def fetch(args):
            self.calls.reserve("source")
            sequence = self.calls.state["used"]["source"]
            started_at = now_iso()
            started = time.monotonic()
            record = {
                "sequence": sequence,
                "operation": source.original_name,
                "server": source.server,
                "arguments": args,
                "started_at": started_at,
                "status": "running",
            }
            try:
                result = await original(args)
            except Exception as exc:
                record.update(
                    status="error",
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    error_type=type(exc).__name__,
                    error_code=getattr(exc, "code", None),
                )
                self.records.append(record)
                save_json(self.root / "source_calls.json", self.records)
                raise
            record.update(
                status="success",
                elapsed_seconds=round(time.monotonic() - started, 3),
                response_hash=digest(result),
            )
            self.records.append(record)
            save_json(self.root / "source_calls.json", self.records)
            save_json(
                self.root / "source_calls" / f"{sequence:03}-{source.original_name}.json",
                record,
            )
            return result

        return replace(source, fetch=fetch)


def build_manifest(
    settings: HostSettings,
    token_limit: int,
    *,
    report_id=REPORT_ID,
    policy_path=Path("config/execution_policy.toml"),
) -> dict[str, Any]:
    from research_copilot.runtime.preflight import load_execution_policy

    policy = load_execution_policy(policy_path)
    if token_limit > policy["token_limit"]:
        raise ValueError("Token limit exceeds execution policy")
    models = load_models_config(PROJECT_ROOT / "config/models.example.toml").model_dump()
    deepseek = models["providers"].get("deepseek")
    if isinstance(deepseek, dict):
        deepseek["base_url"] = settings.deepseek_base_url
    return {
        "report_id": report_id,
        "actor_id": ACTOR_ID,
        "policy_version": POLICY_VERSION,
        "question": QUESTION,
        "token_limit": token_limit,
        "limits": {"model": policy["call_limit"], "source": 1},
        "stage_limits": policy["stages"],
        "prices": policy.get("prices", {}),
        "timeout": 7200,
        "models": models,
        "source": "financekb",
        "automatic_approval": False,
        "test_policy": str(TEST_POLICY.relative_to(PROJECT_ROOT)),
        "attachment": str(ATTACHMENT.relative_to(PROJECT_ROOT)),
        "design": {
            "target": "五粮液(000858.SZ)",
            "theme": "2024年报业绩点评：增收是否增利、费用投放与增长质量",
            "expected_output": "flash_comment；标题、摘要、正文分析、结论/风险提示；证据引用与计算指标",
        },
    }


def control_summary(state, root: Path) -> dict[str, Any]:
    data = state.role_data
    evidence = []
    for ident, record in data.get("evidence", {}).items():
        raw = record.get("raw")
        title = ""
        url = ""
        body_chars = 0
        if isinstance(raw, dict):
            title = str(raw.get("title", ""))[:300]
            url = str(raw.get("url", ""))[:500]
            body = raw.get("body")
            if isinstance(body, str):
                body_chars = len(body)
        evidence.append(
            {
                "evidence_id": ident,
                "kind": record.get("kind"),
                "content_hash": record.get("content_hash"),
                "source": record.get("source", {}).get("server"),
                "title": title,
                "url": url,
                "collected_at": record.get("collected_at"),
                "body_chars": body_chars,
            }
        )
    objects = []
    for ident, obj in data.get("objects", {}).items():
        payload = obj.get("payload", {})
        objects.append(
            {
                "artifact_id": ident,
                "kind": obj.get("kind"),
                "ref": obj.get("ref"),
                "dispatch_id": obj.get("dispatch_id"),
                "keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
                "document_chars": len(payload.get("document", ""))
                if isinstance(payload, dict)
                else 0,
            }
        )
    executions = []
    for ident, record in data.get("executions", {}).items():
        executions.append(
            {
                "execution_id": ident,
                "dispatch_id": record.get("dispatch_id"),
                "status": record.get("status"),
                "command": record.get("command"),
                "exit_code": record.get("exit_code"),
                "seconds": record.get("seconds"),
                "output_chars": len(record.get("output", "") or ""),
                "output_preview": (record.get("output", "") or "")[:2000],
                "response": record.get("response"),
            }
        )
    reviews = data.get("reviews", {})
    task_results = data.get("task_results", {})
    dispatches = state.dispatches
    summary = {
        "report_id": None,
        "artifacts": {kind: ref.model_dump() for kind, ref in state.artifacts.items()},
        "approved": {kind: ref.model_dump() for kind, ref in state.approved.items()},
        "plans": state.plans,
        "reviewed_report": state.reviewed_report.model_dump()
        if state.reviewed_report
        else None,
        "dispatches": dispatches,
        "task_results": task_results,
        "reviews": reviews,
        "objects": objects,
        "evidence": evidence,
        "executions": executions,
        "web_requests": data.get("web_requests", {}),
        "metrics": data.get("metrics", {}),
        "charts": data.get("charts", {}),
        "fact_sheets": {
            key: {
                "dispatch_id": sheet.get("dispatch_id"),
                "fact_count": len(sheet.get("facts", [])),
                "missing_required": sheet.get("missing_required"),
                "missing_optional": sheet.get("missing_optional"),
                "evidence_ids": sheet.get("evidence_ids"),
                "gaps": sheet.get("gaps"),
            }
            for key, sheet in data.get("fact_sheets", {}).items()
        },
        "model_audit": data.get("model_audit", []),
        "source_failures": data.get("source_failures", {}),
        "rejected_task_results": data.get("rejected_task_results", {}),
    }
    save_json(root / "control_summary.json", summary)
    return summary


def extract_artifacts(state, root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    report_ref = state.artifacts.get("report")
    if report_ref:
        obj = state.role_data.get("objects", {}).get(report_ref.artifact_id)
        if obj:
            payload = obj.get("payload", {})
            document = payload.get("document", "")
            (root / "final_report.md").write_text(document, encoding="utf-8")
            save_json(root / "report_payload.json", payload)
            result.update(
                report_artifact=report_ref.model_dump(),
                report_chars=len(document),
                report_headings=[
                    line.strip()
                    for line in document.splitlines()
                    if line.startswith("## ")
                ],
                report_citations=sorted(
                    {token for token in re.findall(r"\[\^(E-[0-9]{6,})\]", document)}
                ),
            )
    analysis_ref = state.artifacts.get("analysis")
    if analysis_ref:
        obj = state.role_data.get("objects", {}).get(analysis_ref.artifact_id)
        if obj:
            payload = obj.get("payload", {})
            save_json(root / "analysis_payload.json", payload)
            result.update(
                analysis_artifact=analysis_ref.model_dump(),
                analysis_summary=payload.get("summary", ""),
                analysis_conclusions=payload.get("conclusions", []),
                analysis_evidence_ids=payload.get("evidence_ids", []),
                analysis_metric_ids=payload.get("metric_ids", []),
                analysis_chart_ids=payload.get("chart_ids", []),
                analysis_assumptions=payload.get("assumptions", []),
                analysis_limitations=payload.get("limitations", []),
            )
    report_review = None
    for review in state.role_data.get("reviews", {}).values():
        if report_ref and review.get("reviewed") == report_ref.model_dump():
            report_review = review
    if report_review:
        save_json(root / "review.json", report_review)
        result["review"] = report_review
    return result


def _role_records(
    records: list[dict[str, Any]], roles: set[str]
) -> list[dict[str, Any]]:
    return [record for record in records if record.get("role") in roles]


def _stage_time(
    records: list[dict[str, Any]], sources: list[dict[str, Any]]
) -> tuple[str | None, str | None, float]:
    entries = [entry for entry in [*records, *sources] if entry.get("started_at")]
    if not entries:
        return None, None, 0.0
    start = min(entries, key=lambda entry: parse_iso(entry["started_at"]))["started_at"]
    end_datetime = max(
        parse_iso(entry["started_at"])
        + timedelta(seconds=float(entry.get("elapsed_seconds", 0.0) or 0.0))
        for entry in entries
    )
    elapsed = sum(float(entry.get("elapsed_seconds", 0.0) or 0.0) for entry in entries)
    return start, end_datetime.isoformat(), round(elapsed, 2)


def build_stage_records(
    state,
    model_records: list[dict[str, Any]],
    source_records: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    history: list[dict[str, Any]],
    extracted: dict[str, Any],
) -> list[dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    task_results = state.role_data.get("task_results", {})
    dispatches = state.dispatches
    plan = state.plans[-1]["plan"] if state.plans else None
    plan_ref = state.artifacts.get("plan")

    supervisor_records = _role_records(model_records, {"supervisor"})
    searcher_records = _role_records(model_records, {"searcher"})
    analysist_records = _role_records(model_records, {"analysist"})
    writer_records = _role_records(model_records, {"writer"})
    reviewer_records = _role_records(model_records, {"reviewer"})
    summarizer_records = _role_records(model_records, {"summarizer"})
    health_records = _role_records(model_records, {"health"})

    def dispatch_rows(role: str) -> list[dict[str, Any]]:
        rows = []
        for dispatch_id, result in task_results.items():
            binding = dispatches.get(dispatch_id, {})
            if binding.get("role") != role:
                continue
            rows.append(
                {
                    "dispatch_id": dispatch_id,
                    "role": role,
                    "status": result.get("status"),
                    "summary": result.get("summary"),
                    "gaps": result.get("gaps"),
                    "inputs": binding.get("inputs"),
                    "references": result.get("references"),
                    "task_brief": state.role_data.get("scopes", {})
                    .get(dispatch_id, {})
                    .get("task_brief"),
                }
            )
        return rows

    start, end, elapsed = _stage_time(health_records, [])
    stages.append(
        {
            "name": "0. 模型连通性",
            "status": "success"
            if health_records
            and all(r.get("status") == "success" for r in health_records)
            else "not_run",
            "started_at": start,
            "finished_at": end,
            "elapsed_seconds": elapsed,
            "input_summary": "向 supervisor 模型发送最小真实请求：只回复 OK",
            "output_summary": "; ".join(
                f"{r.get('model')} -> {r.get('finish_reason')} / {r.get('usage_tokens')} tokens"
                for r in health_records
            ),
            "details": {"calls": health_records},
        }
    )

    start, end, elapsed = _stage_time(supervisor_records, [])
    stages.append(
        {
            "name": "1. 规划与计划确认",
            "status": "success"
            if plan
            and plan.get("report_type") == "flash_comment"
            and plan_ref
            and state.plans
            and plan_ref.model_dump() == state.plans[-1].get("artifact")
            else "partial",
            "started_at": start,
            "finished_at": end,
            "elapsed_seconds": elapsed,
            "input_summary": QUESTION[:1200],
            "output_summary": json.dumps(plan, ensure_ascii=False)[:2000]
            if plan
            else "未生成计划",
            "details": {
                "plan": plan,
                "plan_artifact": plan_ref.model_dump() if plan_ref else None,
                "supervisor_calls": supervisor_records,
                "decisions": [d for d in decisions if d.get("kind") == "confirm_plan"],
            },
        }
    )

    searcher_rows = dispatch_rows("searcher")
    start, end, elapsed = _stage_time(searcher_records, source_records)
    searcher_ok = bool(searcher_rows) and all(
        row["status"] in {"completed", "partial"} for row in searcher_rows
    )
    stages.append(
        {
            "name": "2. 检索与数据获取",
            "status": "success"
            if searcher_ok
            else "partial"
            if searcher_rows
            else "not_run",
            "started_at": start,
            "finished_at": end,
            "elapsed_seconds": elapsed,
            "input_summary": json.dumps(
                [row["task_brief"] or row["inputs"] for row in searcher_rows],
                ensure_ascii=False,
            )[:2000],
            "output_summary": json.dumps(
                [
                    {
                        "dispatch_id": row["dispatch_id"],
                        "status": row["status"],
                        "summary": row["summary"],
                        "gaps": row["gaps"],
                    }
                    for row in searcher_rows
                ],
                ensure_ascii=False,
            )[:3000],
            "details": {
                "dispatches": searcher_rows,
                "model_calls": searcher_records,
                "source_calls": source_records,
                "evidence_count": len(state.role_data.get("evidence", {})),
            },
        }
    )

    analysist_rows = dispatch_rows("analysist")
    executions = [
        record
        for record in state.role_data.get("executions", {}).values()
        if record.get("dispatch_id") in {row["dispatch_id"] for row in analysist_rows}
    ]
    start, end, elapsed = _stage_time(analysist_records, [])
    stages.append(
        {
            "name": "3. 分析与沙盒计算",
            "status": "success"
            if analysist_rows
            and all(row["status"] == "completed" for row in analysist_rows)
            else "partial"
            if analysist_rows
            else "not_run",
            "started_at": start,
            "finished_at": end,
            "elapsed_seconds": elapsed,
            "input_summary": f"授权证据 {len(state.role_data.get('evidence', {}))} 条；"
            + json.dumps(
                [row["task_brief"] or row["inputs"] for row in analysist_rows],
                ensure_ascii=False,
            )[:1500],
            "output_summary": json.dumps(
                {
                    "analysis": extracted.get("analysis_summary"),
                    "conclusions": extracted.get("analysis_conclusions"),
                    "metric_ids": extracted.get("analysis_metric_ids"),
                    "assumptions": extracted.get("analysis_assumptions"),
                    "limitations": extracted.get("analysis_limitations"),
                    "executions": [
                        {
                            "execution_id": item.get("execution_id"),
                            "status": item.get("status"),
                            "exit_code": item.get("exit_code"),
                            "seconds": item.get("seconds"),
                            "output_preview": (item.get("output") or "")[:800],
                        }
                        for item in executions
                    ],
                },
                ensure_ascii=False,
            )[:4000],
            "details": {
                "dispatches": analysist_rows,
                "model_calls": analysist_records,
                "executions": executions,
                "decisions": [
                    d for d in decisions if d.get("kind") == "confirm_analysis"
                ],
            },
        }
    )

    writer_rows = dispatch_rows("writer")
    start, end, elapsed = _stage_time(writer_records, [])
    stages.append(
        {
            "name": "4. 撰写与草稿提交",
            "status": "success"
            if writer_rows
            and all(row["status"] == "completed" for row in writer_rows)
            and extracted.get("report_artifact")
            else "partial"
            if writer_rows
            else "not_run",
            "started_at": start,
            "finished_at": end,
            "elapsed_seconds": elapsed,
            "input_summary": json.dumps(
                [
                    {
                        "dispatch_id": row["dispatch_id"],
                        "task_brief": row["task_brief"],
                        "inputs": row["inputs"],
                    }
                    for row in writer_rows
                ],
                ensure_ascii=False,
            )[:2000],
            "output_summary": json.dumps(
                {
                    "report_artifact": extracted.get("report_artifact"),
                    "report_chars": extracted.get("report_chars"),
                    "headings": extracted.get("report_headings"),
                    "citations": extracted.get("report_citations"),
                    "dispatches": [
                        {
                            "dispatch_id": row["dispatch_id"],
                            "status": row["status"],
                            "summary": row["summary"],
                            "gaps": row["gaps"],
                        }
                        for row in writer_rows
                    ],
                },
                ensure_ascii=False,
            )[:3000],
            "details": {
                "dispatches": writer_rows,
                "model_calls": writer_records,
            },
        }
    )

    reviewer_rows = dispatch_rows("reviewer")
    start, end, elapsed = _stage_time(reviewer_records, [])
    review = extracted.get("review") or {}
    stages.append(
        {
            "name": "5. 独立审核",
            "status": "success"
            if review.get("outcome") == "passed"
            else "partial"
            if reviewer_rows
            else "not_run",
            "started_at": start,
            "finished_at": end,
            "elapsed_seconds": elapsed,
            "input_summary": json.dumps(
                [row["inputs"] for row in reviewer_rows], ensure_ascii=False
            )[:1500],
            "output_summary": json.dumps(
                {
                    "review_id": review.get("review_id"),
                    "outcome": review.get("outcome"),
                    "pass_confidence": review.get("pass_confidence"),
                    "checks_complete": review.get("checks_complete"),
                    "issue_count": len(review.get("issues", [])),
                    "issues": review.get("issues", [])[:8],
                    "summary": review.get("summary"),
                },
                ensure_ascii=False,
            )[:4000],
            "details": {
                "dispatches": reviewer_rows,
                "model_calls": reviewer_records,
                "decisions": [
                    d for d in decisions if d.get("kind") == "reviewer_deep_review"
                ],
            },
        }
    )

    start, end, elapsed = _stage_time(
        [r for r in summarizer_records if r.get("status") == "success"],
        [],
    )
    stages.append(
        {
            "name": "6. 上下文压缩与结构化输出",
            "status": "success" if summarizer_records else "not_triggered",
            "started_at": start,
            "finished_at": end,
            "elapsed_seconds": elapsed,
            "input_summary": f"summarizer 调用 {len(summarizer_records)} 次",
            "output_summary": f"最终报告 {extracted.get('report_chars', 0)} 字符；"
            f"标题层级 {len(extracted.get('report_headings', []))} 个",
            "details": {
                "summarizer_calls": summarizer_records,
                "final_report": extracted.get("report_artifact"),
                "signoff": [d for d in decisions if d.get("kind") == "signoff_report"],
            },
        }
    )
    return stages


def timing_summary(events, model_records, executions):
    waiting = 0.0
    pause = None
    for event in events:
        stamp = parse_iso(event["at"])
        if event["kind"] == "paused":
            pause = stamp
        elif event["kind"] == "resumed" and pause is not None:
            waiting += max(0, (stamp - pause).total_seconds())
            pause = None
    wall = (
        max(
            0,
            (parse_iso(events[-1]["at"]) - parse_iso(events[0]["at"])).total_seconds(),
        )
        if events
        else 0
    )
    return {
        "wall_seconds": wall,
        "human_wait_seconds": waiting,
        "model_cumulative_seconds": sum(
            r.get("elapsed_seconds") or 0 for r in model_records
        ),
        "execution_cumulative_seconds": sum(r.get("seconds") or 0 for r in executions),
        "note": "Cumulative model/execution time can overlap across parallel branches.",
    }


async def export_failed_run(store, report, actor, root, error):
    """Read durable state after failure; never rerun work or modify the ledger."""
    from dataclasses import asdict
    from research_copilot.runtime.acceptance import evaluate_acceptance

    record = await store.load(report, actor)
    context = RunContext(
        actor, report, record.run_id, record.budget_id, record.policy_version
    )
    state = await store.read_control(context)
    budget = await store.load_budget(context)
    control_summary(state, root)
    save_json(root / "durable-session.json", asdict(record))
    save_json(root / "token_budget.json", budget.snapshot())
    acceptance = evaluate_acceptance(state, budget)
    save_json(root / "acceptance.json", acceptance)
    payload = {
        "report_id": report,
        "status": record.status,
        "failure": {
            "type": type(error).__name__,
            "message": str(error),
            "details": getattr(error, "details", {}),
        },
        "budget": budget.snapshot(),
        "acceptance": acceptance,
        "generated_at": now_iso(),
    }
    events_path = root / "timing_events.json"
    if events_path.exists():
        events = json.loads(events_path.read_text("utf-8"))
        events.append({"kind": "failed", "at": now_iso()})
        save_json(events_path, events)
        model_path = root / "model_calls.json"
        models = (
            json.loads(model_path.read_text("utf-8")) if model_path.exists() else []
        )
        payload["timing"] = timing_summary(
            events, models, state.role_data.get("executions", {}).values()
        )
    save_json(root / "test_record.json", payload)


async def run(args: argparse.Namespace) -> None:
    root = Path(args.output or f"output/{args.report_id}").resolve()
    root.mkdir(parents=True, exist_ok=True)
    settings = HostSettings()
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text("utf-8"))
    else:
        manifest = build_manifest(
            settings,
            args.token_limit,
            report_id=args.report_id,
            policy_path=args.execution_policy,
        )
        save_json(manifest_path, manifest)

    if args.action == "prepare":
        print(json.dumps({"prepared": str(root), "report_id": manifest["report_id"]}))
        return

    if not settings.database_url:
        raise RuntimeError("RC_DATABASE_URL is required for the PostgreSQL runtime")
    if not settings.deepseek_api_key:
        raise RuntimeError("RC_DEEPSEEK_API_KEY is required")
    if not settings.financekb_base_url or not settings.financekb_api_token:
        raise RuntimeError(
            "RC_FINANCEKB_BASE_URL and RC_FINANCEKB_API_TOKEN are required"
        )

    if not args.live_authorized:
        raise ValueError(
            "Live model/source/database/Docker execution requires --live-authorized"
        )
    if manifest["report_id"] != args.report_id:
        raise ValueError(
            "Output manifest belongs to another report; cannot reset or reuse its ledger"
        )
    from research_copilot.runtime.preflight import preflight

    _, preflight_result = preflight(
        rules_path=TEST_POLICY, mode="test", policy_path=args.execution_policy
    )

    os.environ["RC_DEEPSEEK_API_KEY"] = settings.deepseek_api_key.get_secret_value()
    timing_path = root / "timing_events.json"
    timing_events = (
        json.loads(timing_path.read_text("utf-8")) if timing_path.exists() else []
    )
    timing_events.append(
        {"kind": "resumed" if args.action == "resume" else "started", "at": now_iso()}
    )
    save_json(timing_path, timing_events)
    calls = CallBudget(root / "calls.json", manifest["limits"])
    resume_existing = args.action == "resume"
    model_log = ModelCallLog(
        root,
        calls,
        json.loads((root / "model_calls.json").read_text("utf-8"))
        if resume_existing and (root / "model_calls.json").exists()
        else None,
    )
    source_log = SourceCallLog(
        root,
        calls,
        json.loads((root / "source_calls.json").read_text("utf-8"))
        if resume_existing and (root / "source_calls.json").exists()
        else None,
    )
    history: list[dict[str, Any]] = []
    if resume_existing and (root / "events.jsonl").exists():
        for line in (root / "events.jsonl").read_text("utf-8").splitlines():
            if line.strip():
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    def on_event(event):
        event = {**event, "recorded_at": now_iso()}
        history.append(event)
        with (root / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        print(json.dumps(event, ensure_ascii=False, default=str), flush=True)

    config = ModelsConfig.model_validate(manifest["models"])
    report, actor = manifest["report_id"], manifest["actor_id"]
    policy_version = manifest["policy_version"]

    async with postgres_runtime(settings.database_url, initialize=True) as (
        store,
        checkpointer,
    ):
        async with open_models(config) as models, AsyncExitStack() as sources_stack:
            bundle = await sources_stack.enter_async_context(
                open_financekb_sources(
                    settings.financekb_base_url,
                    settings.financekb_api_token.get_secret_value(),
                    ca_bundle=settings.financekb_ca_bundle,
                    call_timeout=settings.financekb_call_timeout_seconds,
                )
            )
            source_tools = tuple(source_log.wrap(source) for source in bundle.tools)
            try:
                # Connectivity is exercised by metered business requests; no unbilled ping.
                health = {
                    "status": "not_run",
                    "reason": "all live calls use the shared ledger",
                }
                save_json(root / "health.json", health)

                for role, model in models.items():
                    model_log.wrap(role, model)

                business = BusinessCoordinator(store)
                test_rules = load_report_rules(TEST_POLICY).snapshot()
                services = RoleServices(
                    repository=store,
                    workspace_root=root / "workspace",
                    business=business,
                    report_rules=test_rules,
                )
                services.preflight_result = preflight_result
                try:
                    record = await store.load(report, actor)
                    existing = True
                except LookupError:
                    record = SessionRecord(report, actor, RUNTIME_VERSION, policy_version, report)
                    await store.create(
                        record,
                        TokenBudget(
                            limit=manifest["token_limit"],
                            budget_id=report,
                            call_limit=manifest["limits"]["model"],
                            stage_limits=manifest.get("stage_limits", {}),
                            prices=manifest.get("prices", {}),
                        ).snapshot(),
                    )
                    existing = False
                    workspace_files = WorkspaceFiles(
                        store=store,
                        workspace_root=root / "workspace",
                        policy_version=policy_version,
                    )
                    input_file = await workspace_files.import_input(
                        report,
                        actor,
                        ATTACHMENT.name,
                        ATTACHMENT.read_bytes(),
                    )
                    context = RunContext(
                        actor, report, record.run_id, report, policy_version
                    )
                    regression_issues = json.loads(
                        (
                            PROJECT_ROOT / "tests/fixtures/wuliangye_r4/review.json"
                        ).read_text("utf-8")
                    )["issues"]
                    criteria = json.loads(
                        (
                            PROJECT_ROOT
                            / "tests/fixtures/wuliangye_r4/acceptance_criteria.json"
                        ).read_text("utf-8")
                    )
                    await store.mutate_control(
                        context,
                        lambda state: state.role_data.update(
                            regression_issues=regression_issues,
                            regression_criteria=criteria,
                        ),
                    )
                    receipt = await services.register_attachment(context, input_file)
                    save_json(
                        root / "attachment_receipt.json",
                        {
                            "input": {
                                "report_id": input_file.report_id,
                                "path": input_file.path,
                                "content_hash": input_file.content_hash,
                                "byte_size": input_file.byte_size,
                            },
                            "receipt": receipt,
                        },
                    )

                factory = SupervisorFactory(
                    models=models,
                    store=store,
                    business=business,
                    checkpointer=checkpointer,
                    workspace_root=root / "workspace",
                    resources_root=bundled_resources(),
                    role_services=services,
                    source_tools=source_tools,
                    source_note=bundle.note,
                    source_catalog=bundle.catalog,
                    source_contract=bundle.contract,
                )
                store.resume_validator = business.validate_resume
                session = SessionService(
                    store=store,
                    agent_factory=factory,
                    policy_version=policy_version,
                    on_event=on_event,
                )

                status = await asyncio.wait_for(
                    session.get_status(report, actor)
                    if existing and args.action == "resume"
                    else session.start(report, actor, manifest["question"]),
                    manifest["timeout"],
                )
                if existing and args.action == "resume" and status.status != "paused":
                    status = await asyncio.wait_for(
                        session.resume(report, actor, None), manifest["timeout"]
                    )
                decisions_log = (
                    json.loads((root / "decision_history.json").read_text("utf-8"))
                    if (root / "decision_history.json").exists()
                    else []
                )
                status_history = (
                    json.loads((root / "status_history.json").read_text("utf-8"))
                    if (root / "status_history.json").exists()
                    else []
                )
                cycle = 0
                while True:
                    record_now = await store.load(report, actor)
                    context = RunContext(
                        actor,
                        report,
                        record_now.run_id,
                        record_now.budget_id,
                        policy_version,
                    )
                    state = await store.read_control(context)
                    budget = await store.load_budget(context)
                    status_history.append(
                        {
                            "cycle": cycle,
                            "status": status.status,
                            "interrupts": list(status.interrupts),
                            "recorded_at": now_iso(),
                        }
                    )
                    save_json(root / "status.json", status_history[-1])
                    save_json(root / "status_history.json", status_history)
                    control_summary(state, root)
                    save_json(root / "token_budget.json", budget.snapshot())
                    if status.status != "paused":
                        break
                    if cycle >= args.max_cycles:
                        save_json(
                            root / "failure.txt",
                            {"error": "max approval cycles reached", "cycle": cycle},
                        )
                        break
                    decisions = await build_decisions(
                        status,
                        state,
                        budget,
                        root,
                        decisions_log,
                        stop_after_review=args.stop_after_review,
                        supplied_decisions=json.loads(
                            Path(args.decisions).read_text("utf-8")
                        )
                        if args.decisions
                        else None,
                    )
                    save_json(root / "decision_history.json", decisions_log)
                    if decisions is None:
                        save_json(
                            root / "failure.txt",
                            {
                                "status": "awaiting_operator_decision",
                                "interrupts": list(status.interrupts),
                            },
                        )
                        break
                    save_json(
                        root
                        / "decisions"
                        / f"{len(decisions_log):04}-human-decisions.json",
                        decisions,
                    )
                    status = await asyncio.wait_for(
                        session.resume(report, actor, decisions),
                        manifest["timeout"],
                    )
                    cycle += 1
                record_now = await store.load(report, actor)
                context = RunContext(
                    actor,
                    report,
                    record_now.run_id,
                    record_now.budget_id,
                    policy_version,
                )
                state = await store.read_control(context)
                budget = await store.load_budget(context)
                control_summary(state, root)
                save_json(root / "token_budget.json", budget.snapshot())
                extracted = extract_artifacts(state, root)
                save_json(root / "extracted_artifacts.json", extracted)
                stages = build_stage_records(
                    state,
                    model_log.records,
                    source_log.records,
                    decisions_log,
                    status_history,
                    extracted,
                )
                timing_events.append(
                    {
                        "kind": "paused" if status.status == "paused" else "finished",
                        "at": now_iso(),
                    }
                )
                save_json(timing_path, timing_events)
                record_payload = {
                    "timing": timing_summary(
                        timing_events,
                        model_log.records,
                        state.role_data.get("executions", {}).values(),
                    ),
                    "test_case": manifest["design"],
                    "question": manifest["question"],
                    "manifest": manifest,
                    "health": health,
                    "status_history": status_history,
                    "decisions": decisions_log,
                    "stages": stages,
                    "extracted": extracted,
                    "model_calls": model_log.records,
                    "source_calls": source_log.records,
                    "budget": budget.snapshot(),
                    "call_budget": calls.state,
                    "policy": test_rules,
                    "attachment": json.loads(
                        (root / "attachment_receipt.json").read_text("utf-8")
                    )
                    if (root / "attachment_receipt.json").exists()
                    else None,
                    "generated_at": now_iso(),
                }
                from research_copilot.runtime.acceptance import evaluate_acceptance

                record_payload["acceptance"] = evaluate_acceptance(state, budget)
                save_json(root / "acceptance.json", record_payload["acceptance"])
                save_json(root / "test_record.json", record_payload)
                save_json(
                    root / "e2e_test_record.json",
                    record_payload,
                )
                print(
                    json.dumps(
                        {
                            "report": str(root / "final_report.md"),
                            "status": status_history[-1]["status"]
                            if status_history
                            else None,
                            "cycles": cycle,
                            "model_calls": len(model_log.records),
                            "source_calls": len(source_log.records),
                            "budget": budget.snapshot(),
                        },
                        ensure_ascii=False,
                    )
                )
            except Exception as error:
                (root / "failure.txt").write_text(
                    traceback.format_exc(), encoding="utf-8"
                )
                try:
                    await export_failed_run(store, report, actor, root, error)
                except Exception as export_error:
                    save_json(
                        root / "failure-export-error.json",
                        {
                            "type": type(export_error).__name__,
                            "message": str(export_error),
                        },
                    )
                raise
            finally:
                await sources_stack.aclose()


async def build_decisions(
    status,
    state,
    budget,
    root: Path,
    decisions_log: list[dict[str, Any]],
    *,
    stop_after_review: bool = False,
    supplied_decisions: dict | None = None,
) -> dict[str, Any] | None:
    decisions: dict[str, Any] = {}
    if stop_after_review and any(
        review.get("outcome") == "changes_requested"
        for review in state.role_data.get("reviews", {}).values()
    ):
        for item in status.interrupts:
            payload = item.get("value") or {}
            if any(
                action.get("name") == "confirm_rework"
                for action in payload.get("action_requests", [])
            ):
                save_json(
                    root / "stopped-at-review.json",
                    {
                        "reason": "independent review requested changes; test stops before rework",
                        "interrupts": list(status.interrupts),
                        "recorded_at": now_iso(),
                    },
                )
                return None
    for item in status.interrupts:
        ident = item.get("id")
        payload = item.get("value") or {}
        if "action_requests" in payload:
            action_decisions = []
            rationales = []
            approved_all = True
            for action in payload["action_requests"]:
                name = action["name"]
                artifact = ArtifactRef.model_validate(action["args"]["artifact"])
                approved, rationale = evaluate_action(name, artifact, state)
                approved_all = approved_all and approved
                action_decisions.append({"type": "approve" if approved else "reject"})
                rationales.append(
                    f"{name}: {'approve' if approved else 'reject'} — {rationale}"
                )
            decisions[ident] = {"decisions": action_decisions}
            decisions_log.append(
                {
                    "interrupt_id": ident,
                    "kind": ",".join(
                        action["name"] for action in payload["action_requests"]
                    ),
                    "suggested_approval": approved_all,
                    "decision_source": "program_suggestion",
                    "rationale": "\n".join(rationales),
                    "payload": payload,
                    "recorded_at": now_iso(),
                }
            )
        elif payload.get("kind") == "reviewer_deep_review":
            assessment = payload.get("assessment", {})
            snapshot = payload.get("snapshot", {})
            complete = bool(assessment.get("checks_complete"))
            blocking = list(assessment.get("blocking_issue_ids", []))
            score = assessment.get("pass_confidence")
            issues = assessment.get("issues", [])
            if complete and not blocking and score is not None and score >= 0.85:
                decision = "approve"
                comment = (
                    "Program suggestion: reviewer completed all required reads, "
                    f"pass_confidence={score}, no blocking issues."
                )
                dispositions: list[dict[str, Any]] = []
                approved = True
            elif complete:
                decision = "request_changes"
                issue_text = "; ".join(
                    f"{issue.get('issue_id')}: {issue.get('category')} / {issue.get('reason')}"
                    for issue in issues[:8]
                )
                comment = (
                    "Program suggests changes because blocking issues remain: "
                    + issue_text
                )
                dispositions = []
                approved = False
            else:
                decision = "request_changes"
                comment = (
                    "Program cannot recommend approval: reviewer reports incomplete required "
                    "checks. Re-read all quality/compliance targets and return a complete review."
                )
                dispositions = []
                approved = False
            decisions[ident] = {
                "review_session_id": snapshot.get("review_session_id"),
                "snapshot_id": snapshot.get("snapshot_id"),
                "decision": decision,
                "comment": comment,
                "issue_dispositions": dispositions,
            }
            decisions_log.append(
                {
                    "interrupt_id": ident,
                    "kind": "reviewer_deep_review",
                    "suggested_approval": approved,
                    "decision_source": "program_suggestion",
                    "rationale": comment,
                    "payload": {
                        "snapshot_id": snapshot.get("snapshot_id"),
                        "assessment": assessment,
                    },
                    "recorded_at": now_iso(),
                }
            )
        else:
            return None
    save_json(
        root / "decisions" / f"{len(decisions_log):02}-decision-log.json",
        decisions_log,
    )
    # Program suggestions are never human approval. The authenticated runtime
    # still checks each supplied decision against the current interrupt/version.
    save_json(
        root / "pending-human-decisions.json",
        {
            "decision_source": "program_suggestion",
            "interrupts": list(status.interrupts),
            "suggested_decisions": decisions,
            "budget": budget.status(),
        },
    )
    if supplied_decisions is None or set(supplied_decisions) != set(decisions):
        return None
    for item in status.interrupts:
        for action in (item.get("value") or {}).get("action_requests", []):
            if action["name"] == "signoff_report":
                from research_copilot.runtime.acceptance import evaluate_acceptance

                acceptance = evaluate_acceptance(state, budget, require_signature=False)
                if not acceptance["passed"]:
                    raise ValueError(
                        "Acceptance must pass before human signoff: "
                        + "; ".join(acceptance["failures"])
                    )
    save_json(
        root / "human-decisions.json",
        {"decision_source": "operator_file", "decisions": supplied_decisions},
    )
    return supplied_decisions


def evaluate_action(name: str, artifact: ArtifactRef, state) -> tuple[bool, str]:
    if name == "confirm_plan":
        if not state.plans:
            return False, "no proposed plan recorded"
        matches = [
            p["plan"] for p in state.plans if p["artifact"] == artifact.model_dump()
        ]
        if len(matches) != 1:
            return False, "ambiguous plan reference"
        plan = matches[0]
        if plan.get("report_type") != "flash_comment":
            return False, f"report_type={plan.get('report_type')} is not flash_comment"
        if not plan.get("tasks"):
            return False, "plan has no tasks"
        if "五粮液" not in plan.get("objective", "") and "000858" not in plan.get(
            "objective", ""
        ):
            return False, "plan objective does not identify the requested target"
        return True, "plan matches requested target/report type and has bounded tasks"
    if name == "confirm_analysis":
        if state.artifacts.get("analysis") != artifact:
            return False, "analysis artifact is not the current version"
        obj = state.role_data.get("objects", {}).get(artifact.artifact_id)
        if not obj:
            return False, "analysis payload is unavailable"
        payload = obj.get("payload", {})
        if not payload.get("evidence_ids"):
            return False, "analysis cites no evidence"
        if not payload.get("conclusions"):
            return False, "analysis has no conclusions"
        return True, (
            f"analysis saved with {len(payload.get('evidence_ids', []))} evidence ids, "
            f"{len(payload.get('metric_ids', []))} metric ids"
        )
    if name == "confirm_rework":
        if state.artifacts.get("rework") != artifact:
            return False, "rework artifact is not the current version"
        return True, "rework artifact is current and bound to the reviewer feedback"
    if name == "signoff_report":
        if state.artifacts.get("report") != artifact:
            return False, "report artifact is not the current version"
        if state.reviewed_report != artifact:
            return False, "report has not passed independent review"
        if state.approved.get("analysis") != state.artifacts.get("analysis"):
            return False, "analysis is not approved"
        if state.approved.get("plan") != state.artifacts.get("plan"):
            return False, "plan is not approved"
        return (
            True,
            "reviewed report, approved analysis and approved plan are all current",
        )
    return False, f"unsupported confirmation tool: {name}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["prepare", "start", "resume"], default="start", nargs="?"
    )
    parser.add_argument("--output")
    parser.add_argument("--report-id", default=REPORT_ID)
    parser.add_argument(
        "--execution-policy", type=Path, default=Path("config/execution_policy.toml")
    )
    parser.add_argument("--live-authorized", action="store_true")
    parser.add_argument(
        "--decisions",
        type=Path,
        help="Operator-provided decisions bound to current interrupt IDs",
    )
    parser.add_argument("--token-limit", type=int, default=2_100_000)
    parser.add_argument("--max-cycles", type=int, default=20)
    parser.add_argument(
        "--stop-after-review",
        action="store_true",
        help="Stop before approving rework after a changes_requested review.",
    )
    args = parser.parse_args()
    from research_copilot.runtime.loop import run_async

    run_async(run(args))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
