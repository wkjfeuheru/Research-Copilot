"""Conjunctive acceptance for the controlled Wuliangye regression, not production PIT proof."""


def evaluate_acceptance(state, budget, *, require_signature=True):
    from research_copilot.subagents.quality import validate_claims, assess_source
    from research_copilot.runtime.autonomy import analysis_input

    ledger = budget.snapshot()
    failures = []
    if not budget.status()["usage_verified"]:
        failures.append("usage_unknown")
    # 成本门槛跟随会话自身预算，避免硬编码阈值与执行策略脱节。
    if ledger["known_actual_tokens"] > ledger["limit"] or (
        ledger.get("call_limit") is not None
        and ledger["calls"] > ledger["call_limit"]
    ):
        failures.append("cost_limit_exceeded")
    report = state.artifacts.get("report")
    analysis = state.artifacts.get("analysis")
    if not report or not analysis:
        failures.append("artifacts_missing")
    if require_signature and (not report or state.approved.get("report") != report):
        failures.append("human_signoff_missing")
    reviews = [
        r
        for r in state.role_data.get("reviews", {}).values()
        if report and r.get("reviewed") == report.model_dump()
    ]
    review = reviews[-1] if reviews else {}
    if (
        review.get("outcome") != "passed"
        or not review.get("checks_complete")
        or review.get("pass_confidence", 0) < 0.85
        or any(i["severity"] in {"major", "critical"} for i in review.get("issues", []))
    ):
        failures.append("independent_review_not_passed")
    obj = state.role_data.get("objects", {}).get(report.artifact_id) if report else None
    aobj = (
        state.role_data.get("objects", {}).get(analysis.artifact_id)
        if analysis
        else None
    )
    plan = state.artifacts.get("plan")
    plan_ref = plan.model_dump() if plan else None
    scope = (
        state.role_data.get("scopes", {}).get(aobj["dispatch_id"], {}) if aobj else {}
    )
    sheets = [
        sheet
        for ident, sheet in state.role_data.get("fact_sheets", {}).items()
        if ident in scope.get("fact_sheet_ids", []) and sheet.get("plan") == plan_ref
    ]
    cited = set(aobj["payload"].get("evidence_ids", [])) if aobj else set()
    keys = {
        f["input_ref"]["key"]
        for sheet in sheets
        for f in sheet.get("facts", [])
        if f["input_ref"]["evidence_id"] in cited
    }
    criteria = state.role_data.get("regression_criteria")
    if not criteria:
        failures.append("regression_criteria_missing")
    required = set((criteria or {}).get("required_fields", []))
    optional = set((criteria or {}).get("optional_fields", []))
    if len(required) < 11 or len(optional) < 3 or not (required | optional) <= keys:
        failures.append("fact_coverage_incomplete")
    prior_issues = list(state.role_data.get("regression_issues", []))
    if rework := state.artifacts.get("rework"):
        rework_payload = state.role_data["rework"][rework.artifact_id]
        prior_issues.extend(
            state.role_data.get("reviews", {})
            .get(rework_payload.get("review_id"), {})
            .get("issues", [])
        )
    blocking = {
        i["issue_id"] for i in prior_issues if i["severity"] in {"critical", "major"}
    }
    closures = review.get("issue_closures", [])
    if not blocking <= {
        c["issue_id"] for c in closures if c.get("reason") and c.get("quote")
    }:
        failures.append("prior_issues_not_closed")
    if obj and aobj:
        payload = obj["payload"]
        if (
            payload.get("analysis_ref") != analysis.model_dump()
            or analysis_input(state) != analysis
        ):
            failures.append("analysis_binding_invalid")
        mode = state.role_data.get("runtime_manifest", {}).get("preflight") or {}
        if (
            payload.get("rules", {}).get("is_example", True)
            and mode.get("mode") != "test"
        ):
            failures.append("example_rules")
        body = payload["document"].split("\n\n[^E-", 1)[0]
        metrics = state.role_data.get("metrics", {}).get(aobj["dispatch_id"], {})
        selected = {
            k: metrics[k] for k in aobj["payload"]["metric_ids"] if k in metrics
        }
        evidence = {k: state.role_data["evidence"][k] for k in payload["evidence_ids"]}
        from decimal import Decimal

        for expected in (criteria or {}).get("required_metrics", []):
            matches = {
                mid
                for mid, m in selected.items()
                if m.get("semantic_type") == expected["semantic_type"]
                and m.get("unit") == expected["unit"]
                and Decimal(m["value"]) == Decimal(expected["value"])
            }
            if not matches or (
                expected.get("required_in_report")
                and not any(
                    c.get("required_in_report")
                    and matches & set(c.get("metric_ids", []))
                    for c in aobj["payload"].get("claims", [])
                )
            ):
                failures.append(
                    "required_metric_semantics_missing:" + expected["semantic_type"]
                )
        if any(
            phrase not in body
            for phrase in (criteria or {}).get("required_body_phrases", [])
        ):
            failures.append("required_case_expression_missing")
        if validate_claims(
            body,
            payload.get("claims", {}),
            selected,
            evidence,
            aobj["payload"].get("claims", []),
        ):
            failures.append("claim_validation_failed")
        as_of = state.role_data.get("research_as_of")
        if not as_of or any(
            not assess_source(
                r,
                as_of,
                publication=state.role_data.get("verified_publications", {}).get(ident),
            )["eligible"]
            for ident, r in evidence.items()
        ):
            failures.append("source_eligibility_failed")
    else:
        failures.append("payload_missing")
    return {
        "passed": not failures,
        "failures": failures,
        "budget": budget.status(),
        "validation_mode": "controlled_package",
        "historical_availability": "unknown",
        "production_pit_accepted": False,
        "issue_closures": closures,
    }
