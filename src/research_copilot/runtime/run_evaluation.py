"""Read-only, case-bound evaluation of recorded business snapshots.

Scores describe frozen assertions over stored artifacts, not model-judged quality.
No execution, checkpoint construction, approval or provider billing is performed.
"""

from dataclasses import asdict
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research_copilot.runtime.signoff import current_review, signoff_error


class FrozenSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Check(FrozenSpec):
    id: str = Field(min_length=1)
    root: Literal["report", "analysis", "review", "metrics", "facts", "collection"]
    path: list[str | int] = Field(default_factory=list)
    match: Literal["equals", "contains"] = "equals"
    expected: Any


class Case(FrozenSpec):
    id: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    user_request: str
    expected_signed: bool
    checks: list[Check] = Field(min_length=1)


class BudgetLevel(FrozenSpec):
    id: str = Field(min_length=1)
    token_limit: int = Field(gt=0)
    call_limit: int | None = Field(ge=1)
    stage_limits: dict[str, dict[str, int | dict[str, int]]] = Field(
        default_factory=dict
    )


def _unique(items):
    if len(items) != len(set(items)):
        raise ValueError("Duplicate evaluation identity")


class Suite(FrozenSpec):
    format: Literal[1]
    cases: list[Case] = Field(min_length=1)
    budgets: list[BudgetLevel] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_identities(self):
        _unique([case.id for case in self.cases])
        _unique([budget.id for budget in self.budgets])
        for case in self.cases:
            _unique([check.id for check in case.checks])
        return self


class RunBinding(FrozenSpec):
    case_id: str
    budget_id: str
    report_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)


class Bindings(FrozenSpec):
    usage_origin: Literal["synthetic_offline", "recorded_runtime_unverified"]
    runs: list[RunBinding]


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _projection(state):
    from research_copilot.runtime.evaluation_collection import collection_projection

    data = state.role_data

    def artifact(kind):
        ref = state.artifacts.get(kind)
        obj = data.get("objects", {}).get(ref.artifact_id) if ref else None
        return obj if obj and obj.get("ref") == ref.model_dump() else {}

    analysis, report = artifact("analysis"), artifact("report")
    payload = analysis.get("payload", {})
    dispatch = analysis.get("dispatch_id")
    metrics = data.get("metrics", {}).get(dispatch, {})
    scope = data.get("scopes", {}).get(dispatch, {})
    plan = state.artifacts.get("plan")
    facts = [
        fact
        for ident, sheet in data.get("fact_sheets", {}).items()
        if plan
        and sheet.get("plan") == plan.model_dump()
        and ident in scope.get("fact_sheet_ids", [])
        for fact in sheet.get("facts", [])
        if fact.get("input_ref", {}).get("evidence_id")
        in payload.get("evidence_ids", [])
        and fact in payload.get("verified_facts", [])
    ]
    return {
        "report": report.get("payload", {}),
        "analysis": payload,
        "review": current_review(state) or {},
        "metrics": [
            metrics[mid] for mid in payload.get("metric_ids", []) if mid in metrics
        ],
        "facts": facts,
        "collection": collection_projection(state),
    }


def _matches(actual, expected):
    # Dict subsets allow stable truth fields without freezing generated identities.
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _matches(actual[key], value)
            for key, value in expected.items()
        )
    # JSON bool and number are distinct even though Python True == 1.
    return digest(actual) == digest(expected)


def _check(projection, check):
    value = projection[check.root]
    for key in check.path:
        if isinstance(value, dict) and isinstance(key, str) and key in value:
            value = value[key]
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
            value = value[key]
        else:
            return False
    if check.match == "contains":
        return isinstance(value, list) and any(
            _matches(row, check.expected) for row in value
        )
    return digest(value) == digest(check.expected)


async def evaluate_runs(store, actor_id, suite_data, binding_data):
    suite, bindings = (
        Suite.model_validate(suite_data),
        Bindings.model_validate(binding_data),
    )
    suite_hash = digest(suite.model_dump(mode="json"))
    # Validate the entire mapping before any reads or partially written output.
    pairs = {(case.id, budget.id) for case in suite.cases for budget in suite.budgets}
    _unique([(run.case_id, run.budget_id) for run in bindings.runs])
    _unique([run.report_id for run in bindings.runs])
    if any((run.case_id, run.budget_id) not in pairs for run in bindings.runs):
        raise ValueError("Run binding is outside the frozen case/budget matrix")
    mapped = {(run.case_id, run.budget_id): run for run in bindings.runs}
    rows = []
    for level in suite.budgets:
        for case in suite.cases:
            row = {"case_id": case.id, "budget_id": level.id, "scenario": case.scenario}
            binding = mapped.get((case.id, level.id))
            if binding is None:
                rows.append(row | {"observed": False, "reason": "not_run"})
                continue
            view = await store.read_snapshot(binding.report_id, actor_id)
            record, state, ledger = view.record, view.state, view.budget
            if (
                record.user_request != case.user_request
                or record.run_id != binding.run_id
                or record.policy_version != binding.policy_version
                or ledger.limit != level.token_limit
                or ledger.call_limit != level.call_limit
                or ledger.stage_limits != level.stage_limits
            ):
                raise ValueError(
                    "Run identity, question or budget differs from evaluation binding"
                )
            status = ledger.status()
            report = state.artifacts.get("report")
            signed = bool(report and state.approved.get("report") == report)
            completed = signed and signoff_error(state, status) is None
            projection = _projection(state)
            checks = [
                {"id": check.id, "passed": _check(projection, check)}
                for check in case.checks
            ]
            rows.append(
                row
                | {
                    "observed": True,
                    "report_id": record.report_id,
                    "run_id": record.run_id,
                    "policy_version": record.policy_version,
                    "ledger_budget_id": record.budget_id,
                    "budget_definition_sha256": digest(
                        {
                            "limit": ledger.limit,
                            "call_limit": ledger.call_limit,
                            "stage_limits": ledger.stage_limits,
                            "prices": ledger.prices,
                        }
                    ),
                    "session_status": record.status,
                    "snapshot_sha256": digest(
                        {
                            "record": asdict(record),
                            "control": state.model_dump(mode="json"),
                            "budget": ledger.snapshot(),
                        }
                    ),
                    "signed": signed,
                    "completed": completed,
                    "checks": checks,
                    "quality_passed": all(item["passed"] for item in checks)
                    and (completed if case.expected_signed else not signed),
                    "usage_verified": status["usage_verified"],
                    "calls": status["calls"],
                    "known_tokens": status["known_actual_tokens"],
                    "reserved_tokens": status["reserved_tokens"],
                    "unknown_reserved_tokens": status["unknown_reserved_tokens"],
                    "unknown_requests": status["unknown_requests"],
                    "estimated_cost": status["estimated_cost"]
                    if status["cost_complete"]
                    else None,
                    "real_model_cost": None,
                }
            )
    curves = []
    for level in suite.budgets:
        observed = [
            row for row in rows if row["budget_id"] == level.id and row["observed"]
        ]
        curves.append(
            level.model_dump()
            | {
                "expected_runs": len(suite.cases),
                "observed_runs": len(observed),
                "coverage": len(observed) / len(suite.cases),
                "quality_pass_rate": sum(row["quality_passed"] for row in observed)
                / len(suite.cases)
                if observed
                else None,
                "agent_completion_rate": sum(row["completed"] for row in observed)
                / len(suite.cases)
                if observed
                else None,
                "known_tokens": sum(row["known_tokens"] for row in observed)
                if observed
                else None,
                "calls": sum(row["calls"] for row in observed) if observed else None,
                "usage_complete": len(observed) == len(suite.cases)
                and all(row["usage_verified"] for row in observed),
            }
        )
    return {
        "format": 1,
        "scope": "stored_artifact_checks",
        "suite_sha256": suite_hash,
        "bindings_sha256": digest(bindings.model_dump(mode="json")),
        "usage_origin": bindings.usage_origin,
        "real_model_cost": None,
        "snapshot_consistency": "one_row_per_report_not_atomic_across_reports",
        "complete": all(row["usage_complete"] for row in curves),
        "cases": rows,
        "budget_curve": curves,
    }
