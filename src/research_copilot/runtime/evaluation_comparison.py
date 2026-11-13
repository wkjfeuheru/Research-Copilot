"""Compare frozen assertions and ledger usage, without executing or approving runs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from research_copilot.runtime.run_evaluation import BudgetLevel, Suite, digest


class CheckResult(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str
    passed: bool


class ObservedCase(BaseModel):
    # Other export metadata is retained by the whole-input digest, not interpreted.
    model_config = ConfigDict(strict=True)
    case_id: str
    budget_id: str
    observed: Literal[True]
    report_id: str
    run_id: str
    policy_version: str
    budget_definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_status: str
    signed: bool
    completed: bool
    quality_passed: bool
    usage_verified: bool
    checks: list[CheckResult]
    calls: int = Field(ge=0)
    known_tokens: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)
    unknown_reserved_tokens: int = Field(ge=0)
    unknown_requests: int = Field(ge=0)


def _index(report, suite):
    expected = {
        (case.id, level.id): case for case in suite.cases for level in suite.budgets
    }
    rows, reports = {}, set()
    for raw in report["cases"]:
        key = raw["case_id"], raw["budget_id"]
        if key not in expected or key in rows or type(raw.get("observed")) is not bool:
            raise ValueError("Invalid or duplicate evaluation matrix identity")
        if not raw["observed"]:
            rows[key] = None
            continue
        row = ObservedCase.model_validate(raw)
        if row.report_id in reports:
            raise ValueError("A report cannot fill multiple evaluation cases")
        reports.add(row.report_id)
        case = expected[key]
        checks = {check.id: check.passed for check in row.checks}
        if len(checks) != len(row.checks) or checks.keys() != {
            check.id for check in case.checks
        }:
            raise ValueError("Recorded checks differ from frozen case")
        correct = all(checks.values()) and (
            row.completed if case.expected_signed else not row.signed
        )
        if row.quality_passed != correct or (row.completed and not row.signed):
            raise ValueError("Recorded outcome contradicts case checks or signature")
        rows[key] = row
    if rows.keys() != expected.keys():
        raise ValueError("Evaluation matrix must include every frozen case/budget pair")
    levels = {}
    for raw in report["budget_curve"]:
        level = BudgetLevel.model_validate(
            {key: raw[key] for key in BudgetLevel.model_fields if key in raw}
        )
        if level.id in levels:
            raise ValueError("Duplicate budget curve identity")
        levels[level.id] = level
    if levels != {level.id: level for level in suite.budgets}:
        raise ValueError("Recorded budget levels differ from frozen suite")
    return rows


def compare_runs(suite_data, baseline, candidate):
    suite = Suite.model_validate(suite_data)
    suite_hash = digest(suite.model_dump(mode="json"))
    result = {
        "format": 1,
        "scope": "stored_artifact_comparison",
        "suite_sha256": suite_hash,
        "baseline_sha256": digest(baseline),
        "candidate_sha256": digest(candidate),
        "status": "not_comparable",
        "reasons": [],
        "cases": [],
        "budget_curve": [],
        "real_model_cost_delta": None,
        "execution_configuration_verified": False,
        "regression_policy": "case_checks_and_completion_only",
    }
    reasons = result["reasons"]
    for label, report in (("baseline", baseline), ("candidate", candidate)):
        if report.get("format") != 1 or report.get("scope") != "stored_artifact_checks":
            raise ValueError("Comparison requires stored_artifact_checks format 1")
        if report.get("usage_origin") not in {
            "synthetic_offline",
            "recorded_runtime_unverified",
        }:
            raise ValueError("Unknown recorded usage origin")
        if report.get("suite_sha256") != suite_hash:
            reasons.append(label + ":suite_changed")
    if baseline["usage_origin"] != candidate["usage_origin"]:
        reasons.append("usage_origin_changed")
    if reasons:
        return result
    before, after = _index(baseline, suite), _index(candidate, suite)
    for key in before:
        old, new = before[key], after[key]
        prefix = "/".join(key)
        if old is None or new is None:
            reasons.append(prefix + ":not_run")
            continue
        for label, row in (("baseline", old), ("candidate", new)):
            if (
                not row.usage_verified
                or row.reserved_tokens
                or row.unknown_reserved_tokens
                or row.unknown_requests
            ):
                reasons.append(prefix + ":" + label + ":usage_incomplete")
            if row.session_status in {"created", "running"}:
                reasons.append(prefix + ":" + label + ":run_in_progress")
        if old.policy_version != new.policy_version:
            reasons.append(prefix + ":policy_changed")
        if old.budget_definition_sha256 != new.budget_definition_sha256:
            reasons.append(prefix + ":budget_definition_changed")
    if reasons:
        return result
    rows = []
    for key in before:
        old, new = before[key], after[key]
        old_checks, new_checks = (
            {check.id: check.passed for check in row.checks} for row in (old, new)
        )
        rows.append(
            {
                "case_id": key[0],
                "budget_id": key[1],
                "baseline_report_id": old.report_id,
                "candidate_report_id": new.report_id,
                "baseline_snapshot_sha256": old.snapshot_sha256,
                "candidate_snapshot_sha256": new.snapshot_sha256,
                "quality_regressed": old.quality_passed and not new.quality_passed,
                "completion_regressed": old.completed and not new.completed,
                "regressed_checks": sorted(
                    ident
                    for ident in old_checks
                    if old_checks[ident] and not new_checks[ident]
                ),
                "known_tokens_delta": new.known_tokens - old.known_tokens,
                "calls_delta": new.calls - old.calls,
            }
        )
    curves = []
    for level in suite.budgets:
        old = [row for key, row in before.items() if key[1] == level.id]
        new = [row for key, row in after.items() if key[1] == level.id]
        curves.append(
            {
                "budget_id": level.id,
                "case_count": len(suite.cases),
                "quality_pass_rate_delta": (
                    sum(row.quality_passed for row in new)
                    - sum(row.quality_passed for row in old)
                )
                / len(suite.cases),
                "agent_completion_rate_delta": (
                    sum(row.completed for row in new)
                    - sum(row.completed for row in old)
                )
                / len(suite.cases),
                "known_tokens_delta": sum(row.known_tokens for row in new)
                - sum(row.known_tokens for row in old),
                "calls_delta": sum(row.calls for row in new)
                - sum(row.calls for row in old),
            }
        )
    result.update(
        status="regressed"
        if any(
            row["quality_regressed"]
            or row["completion_regressed"]
            or row["regressed_checks"]
            for row in rows
        )
        else "no_regression",
        usage_origin=baseline["usage_origin"],
        cases=rows,
        budget_curve=curves,
    )
    return result
