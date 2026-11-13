"""Version-bound rework proposals; visible artifacts are never approval receipts."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from research_copilot.contracts import ArtifactRef
from research_copilot.runtime.autonomy import autonomous


class ReworkTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    role: Literal["searcher", "analysist", "writer"]
    objective: str = Field(min_length=1, max_length=8000)
    issue_ids: list[str] = Field(min_length=1, max_length=100)


def pending_review(state):
    current = state.artifacts.get("report")
    return next(
        (
            r
            for r in reversed(list(state.role_data.get("reviews", {}).values()))
            if r["outcome"] == "changes_requested"
            and current
            and r["reviewed"] == current.model_dump()
        ),
        None,
    )


def validate_base(state, payload):
    ref = state.artifacts.get("rework")
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    if not ref or hashlib.sha256(encoded).hexdigest() != ref.content_hash:
        raise ValueError("Rework payload hash mismatch")
    for kind in (
        ("scope", "analysis", "report")
        if autonomous(state)
        else ("plan", "analysis", "report")
    ):
        current = state.artifacts.get(kind)
        if not current or current.model_dump() != payload.get("base_" + kind + "_ref"):
            raise ValueError("Rework base changed: " + kind)


def propose(state, review_id, tasks):
    if "report" in state.approved:
        raise PermissionError("Signed reports cannot be reworked")
    review = pending_review(state)
    if not review or review["review_id"] != review_id:
        raise ValueError("Rework requires the current changes_requested review")
    tasks = [ReworkTask.model_validate(t).model_dump() for t in tasks]
    if not tasks or len(tasks) > 100:
        raise ValueError("Rework requires bounded tasks")
    issue_ids = {i["issue_id"] for i in review["issues"]}
    if any(not set(t["issue_ids"]) <= issue_ids for t in tasks):
        raise ValueError("Unknown rework issue")
    blocking = {
        i["issue_id"]
        for i in review["issues"]
        if i["severity"] in {"critical", "major"}
    }
    if not blocking <= {i for t in tasks for i in t["issue_ids"]}:
        raise ValueError("Rework omits blocking issues")
    payload = {
        "review_id": review_id,
        "tasks": tasks,
        **{
            "base_" + k + "_ref": state.artifacts[k].model_dump()
            for k in (
                ("scope", "analysis", "report")
                if autonomous(state)
                else ("plan", "analysis", "report")
            )
        },
    }
    content_hash = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
    previous = state.artifacts.get("rework")
    if previous and previous.content_hash == content_hash:
        return previous
    ref = ArtifactRef(
        artifact_id="rework:" + review_id + ":" + content_hash[:16],
        version=str(len(state.role_data.get("rework", {})) + 1),
        content_hash=content_hash,
    )
    state.artifacts["rework"] = ref
    state.role_data.setdefault("rework", {})[ref.artifact_id] = payload
    state.approved.pop("rework", None)
    state.approved.pop("report", None)
    state.reviewed_report = None
    state.grants.clear()
    if any(t["role"] in {"searcher", "analysist"} for t in tasks):
        state.approved.pop("analysis", None)
    return ref


def status(state):
    ref = state.artifacts.get("rework")
    if not ref:
        return None
    payload = state.role_data.get("rework", {}).get(ref.artifact_id, {})
    signed = (
        state.artifacts.get("report") is not None
        and state.approved.get("report") == state.artifacts["report"]
    )
    phase = (
        "completed"
        if signed
        else "in_progress"
        if state.approved.get("rework") == ref
        or (autonomous(state) and payload.get("tasks"))
        else "awaiting_confirmation"
        if payload.get("tasks")
        else "awaiting_proposal"
    )
    return {**payload, "artifact": ref.model_dump(), "phase": phase, "signed": signed}


def recover(state, review_id, root):
    from pathlib import PurePosixPath
    from research_copilot.subagents.services import digest
    from research_copilot.workspace.filesystem import reject_links

    if "report" in state.approved:
        raise PermissionError("Signed reports cannot be recovered as rework")
    matches = [
        r
        for r in state.role_data.get("reviews", {}).values()
        if r["review_id"] == review_id
    ]
    if len(matches) != 1 or matches[0]["outcome"] != "changes_requested":
        raise ValueError("Recovery requires one explicit changes_requested review")
    objects = state.role_data.get("objects", {})

    def verified(ref, kind):
        obj = objects.get(ref["artifact_id"])
        if (
            not obj
            or obj["kind"] != kind
            or obj["ref"] != ref
            or digest(obj["payload"]) != ref["content_hash"]
        ):
            raise ValueError("Recovery object identity/hash mismatch")
        path = obj["path"]
        if (
            not path.startswith("/workspace/")
            or ".." in PurePosixPath(path).parts
            or "\\" in path
            or ":" in path
        ):
            raise ValueError("Invalid recovery path")
        raw = reject_links(root / path.removeprefix("/workspace/")).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref["content_hash"]:
            raise ValueError("Recovery file hash mismatch")
        return obj

    report = verified(matches[0]["reviewed"], "report")
    analysis = verified(report["payload"]["analysis_ref"], "analysis")
    for kind, obj in (("report", report), ("analysis", analysis)):
        existing = state.artifacts.get(kind)
        if existing and existing.model_dump() != obj["ref"]:
            raise ValueError("Recovery conflicts with current artifact")
    if autonomous(state):
        from research_copilot.runtime.autonomy import ReportScope

        original_scope = (
            state.dispatches.get(report["dispatch_id"], {})
            .get("inputs", {})
            .get("scope")
        )
        current_scope = state.artifacts.get("scope")
        scope_hash = hashlib.sha256(
            ReportScope.model_validate(state.role_data["report_scope"])
            .model_dump_json()
            .encode()
        ).hexdigest()
        if (
            not current_scope
            or original_scope != current_scope.model_dump()
            or original_scope["content_hash"] != scope_hash
        ):
            raise ValueError("Recovery scope lineage mismatch")
        state.artifacts["scope"] = ArtifactRef.model_validate(original_scope)
    else:
        original_plan = (
            state.dispatches.get(report["dispatch_id"], {})
            .get("inputs", {})
            .get("plan")
        )
        if not original_plan:
            raise ValueError("Recovery requires original plan lineage")
        if original_plan:
            plans = [p for p in state.plans if p["artifact"] == original_plan]
            if len(plans) != 1:
                raise ValueError("Recovery cannot identify original plan")
            from research_copilot.runtime.business import ResearchPlan

            original = ResearchPlan.model_validate(plans[0]["plan"])
            if (
                hashlib.sha256(
                    original.model_dump_json(exclude_unset=True).encode()
                ).hexdigest()
                != original_plan["content_hash"]
            ):
                raise ValueError("Recovery plan hash mismatch")
            state.artifacts["plan"] = ArtifactRef.model_validate(original_plan)
            state.approved.pop("plan", None)
    state.artifacts.update(
        report=ArtifactRef.model_validate(report["ref"]),
        analysis=ArtifactRef.model_validate(analysis["ref"]),
    )
    state.approved.pop("report", None)
    state.approved.pop("analysis", None)
    state.approved.pop("rework", None)
    state.artifacts.pop("rework", None)
    state.reviewed_report = None
    state.grants.clear()
    state.role_data.setdefault("recovery_audit", []).append(
        {
            "review_id": review_id,
            "report_ref": report["ref"],
            "analysis_ref": analysis["ref"],
        }
    )
    return {k: state.artifacts[k].model_dump() for k in ("report", "analysis")}


def validate_dispatch(state, binding):
    """Recheck at use and commit time, including revocation after dispatch."""
    current = state.artifacts.get("rework")
    if binding["inputs"].get("rework") != (current.model_dump() if current else None):
        raise PermissionError("Dispatch rework version changed")
    if not current:
        return
    if not autonomous(state) and state.approved.get("rework") != current:
        raise PermissionError("Current rework requires human confirmation")
    payload = state.role_data["rework"][current.artifact_id]
    if not payload.get("tasks"):
        raise PermissionError("Register a rework proposal before dispatch")
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    if hashlib.sha256(encoded).hexdigest() != current.content_hash:
        raise ValueError("Rework payload hash mismatch")
    roles = {t["role"] for t in payload["tasks"]}
    # Downstream writing/review are necessary to close analysis/collection fixes.
    allowed = roles | {"reviewer", "writer"}
    if "searcher" in roles:
        allowed.add("analysist")
    if binding["role"] not in allowed:
        raise PermissionError("Role is outside the approved rework scope")
