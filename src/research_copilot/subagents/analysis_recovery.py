"""Submission repair grants preserve the immutable origin of computed receipts."""

import copy

from research_copilot.subagents.services import digest


def bind_submission_repair(state, dispatch_id, original_id):
    """Called only inside authorized dispatch admission, never from model input."""
    data = state.role_data
    original, target = state.dispatches[original_id], state.dispatches[dispatch_id]
    if original["role"] != "analysist" or target["role"] != "analysist":
        return
    if original.get("work_group") != target.get("work_group") or original["inputs"].get(
        "scope"
    ) != target["inputs"].get("scope"):
        raise PermissionError("Submission repair scope changed")
    records = {
        key: row
        for key, row in data.get("executions", {}).items()
        if row["dispatch_id"] == original_id and row["status"] == "registered"
    }
    if not records:
        return
    grants = {}
    for kind in ("metrics", "charts"):
        inherited = {}
        for ident, item in data.get(kind, {}).get(original_id, {}).items():
            execution_id = (item if kind == "metrics" else item["receipt"]).get(
                "execution_id"
            )
            record = records.get(execution_id)
            if (
                not record
                or ident
                not in record["response"][
                    "metric_ids" if kind == "metrics" else "chart_ids"
                ]
            ):
                continue
            if not set(
                (item if kind == "metrics" else item["receipt"])["evidence_ids"]
            ) <= set(data["scopes"][dispatch_id]["evidence_ids"]):
                raise PermissionError("Submission repair exceeds evidence scope")
            inherited[ident] = copy.deepcopy(item)
            grants[ident] = {
                "kind": kind,
                "origin_dispatch_id": original_id,
                "execution_id": execution_id,
                "receipt_hash": digest(item),
                "execution_hash": digest(record),
            }
        if inherited:
            data.setdefault(kind, {}).setdefault(dispatch_id, {}).update(inherited)
    if grants:
        data["scopes"][dispatch_id]["submission_repair"] = grants


def validate_receipt_origin(state, dispatch_id, ident, kind):
    """Check both the repair copy and original execution against the admission grant."""
    data = state.role_data
    item = data[kind][dispatch_id][ident]
    execution_id = (item if kind == "metrics" else item["receipt"]).get("execution_id")
    record = data.get("executions", {}).get(execution_id)
    if not record:
        raise ValueError("Invalid execution receipt")
    if record["dispatch_id"] == dispatch_id:
        return record
    grant = data["scopes"][dispatch_id].get("submission_repair", {}).get(ident)
    original_id = record["dispatch_id"]
    origin, target = state.dispatches[original_id], state.dispatches[dispatch_id]
    if (
        not grant
        or grant["kind"] != kind
        or grant["origin_dispatch_id"] != original_id
        or grant["execution_id"] != execution_id
        or data["scopes"][dispatch_id].get("correction_of") != original_id
        or origin.get("work_group") != target.get("work_group")
        or origin["inputs"].get("scope") != target["inputs"].get("scope")
        or grant["receipt_hash"] != digest(item)
        or grant["receipt_hash"] != digest(data[kind][original_id][ident])
        or grant["execution_hash"] != digest(record)
    ):
        raise PermissionError("Invalid submission repair receipt lineage")
    for eid, expected in record.get("evidence_hashes", {}).items():
        source = data["evidence"].get(eid, {})
        if (
            source.get("content_hash") != expected
            or digest(source.get("raw")) != expected
        ):
            raise ValueError("Submission repair evidence hash mismatch")
    return record
