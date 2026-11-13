"""One eligibility policy for scheduling, UI projection and atomic signoff."""


def current_review(state):
    report = state.artifacts.get("report")
    return next(
        (
            review
            for review in reversed(list(state.role_data.get("reviews", {}).values()))
            if report and review["reviewed"] == report.model_dump()
        ),
        None,
    )


def signoff_error(state, budget_status):
    report, analysis, plan = (
        state.artifacts.get(kind) for kind in ("report", "analysis", "plan")
    )
    if not report or state.reviewed_report != report:
        return "Current report has not passed review"
    review = current_review(state)
    if (
        not review
        or review.get("outcome") != "passed"
        or not review.get("checks_complete")
    ):
        return "Current review is missing or incomplete"
    human = review.get("decision_source") == "human"
    feedback = review.get("feedback") or {}
    if human and (
        feedback.get("decision") != "approve"
        or feedback.get("review_session_id") != review.get("review_id")
        or feedback.get("snapshot_id") != review.get("snapshot_id")
    ):
        return "Human review decision is not bound to the current snapshot"
    if not human and review.get("pass_confidence", 0) < 0.85:
        return "Current review confidence is insufficient"
    dispositions = {
        item.get("issue_id"): item for item in feedback.get("issue_dispositions", [])
    }
    for issue in review.get("issues", []):
        if issue.get("hard"):
            return "Hard review issues cannot be overridden"
        if issue["severity"] in {"major", "critical"}:
            disposition = dispositions.get(issue.get("issue_id"), {})
            if (
                not human
                or disposition.get("decision") != "false_positive"
                or not str(disposition.get("reason", "")).strip()
            ):
                return "Blocking review issue has no valid disposition"
    from research_copilot.runtime.autonomy import autonomous

    if autonomous(state):
        if "scope" not in state.artifacts or not analysis:
            return "Current scope and analysis are required"
    else:
        if not plan or state.approved.get("plan") != plan:
            return "Current plan requires human confirmation"
        if not analysis or state.approved.get("analysis") != analysis:
            return "Current analysis requires human confirmation"
    obj = state.role_data.get("objects", {}).get(report.artifact_id)
    if (
        not obj
        or obj.get("ref") != report.model_dump()
        or obj["payload"].get("analysis_ref") != analysis.model_dump()
    ):
        return "Report analysis version is stale or missing"
    if not budget_status or not budget_status.get("usage_verified"):
        return "Model usage must be verified before signoff"
    return None
