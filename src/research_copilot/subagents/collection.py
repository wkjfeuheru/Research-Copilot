"""Dispatch-local collection limits; provider deduplication remains unchanged."""

from research_copilot.subagents.services import digest


def collection_budgets(state):
    """Expose existing shared limits without changing or resetting their ledgers."""
    return {
        ident: {
            "scope": "shared_work_group",
            "used_calls": len(group["calls"]),
            "max_source_calls": group["max_source_calls"],
            "remaining_calls": max(0, group["max_source_calls"] - len(group["calls"])),
            "no_progress_calls": group["no_progress_calls"],
            "max_no_progress_calls": group["max_no_progress_calls"],
        }
        for ident, group in state.role_data.get("collection_work", {}).items()
    }


def collection_stop_reason(scope):
    policy = scope.get("task_brief") or {}
    if scope.get("collection_stop_reason"):
        return scope["collection_stop_reason"]
    if scope.get("no_progress_calls", 0) >= policy.get("max_no_progress_calls", 2):
        return "no_progress"
    if len(scope.get("source_calls", {})) >= policy.get("max_source_calls", 8):
        return "source_exhausted"
    # Source calls bound breadth; model turns also cover task reads, locating
    # rows inside a fetched original and the bounded wrap-up submission.
    turn_limit = max(6, 2 * policy.get("max_source_calls", 8) + 2)
    if scope.get("collection_model_turns", 0) >= turn_limit:
        return "model_turn_limit"
    return None


def collection_messages(messages, *, content_chars=24000):
    """Bound evidence tool history in the model view, never mutate audit messages.

    Omission markers are not evidence. Keep call/result pairs and the newest
    unique pages; the authorized originals remain available via their paths.
    """
    import json
    from langchain_core.messages import AIMessage, ToolMessage

    calls = {
        c["id"]: c for m in messages if isinstance(m, AIMessage) for c in m.tool_calls
    }
    remaining, seen, result = content_chars, set(), []
    for message in reversed(messages):
        call = calls.get(getattr(message, "tool_call_id", None), {})
        if (
            isinstance(message, ToolMessage)
            and call.get("name") in {"read_file", "grep", "ls", "glob"}
            and message.status != "error"
        ):
            identity = digest([call.get("name"), call.get("args"), message.content])
            size = len(str(message.content))
            if identity in seen or size > remaining:
                marker = json.dumps(
                    {
                        "context_omitted": True,
                        "tool": call.get("name"),
                        "arguments": call.get("args"),
                        "content_hash": digest(message.content),
                        "note": "Historical output omitted from model view, not evidence. Original remains in checkpoint; use authorized source paths and bounded pages if needed.",
                    },
                    ensure_ascii=False,
                )
                message = message.model_copy(update={"content": marker})
            else:
                seen.add(identity)
                remaining -= size
        result.append(message)
    return list(reversed(result))


async def begin_source_call(services, context, dispatch, call):
    identity = digest([call["name"], call["args"]])

    def reserve(state):
        scope = state.role_data["scopes"][dispatch.dispatch_id]
        if scope.get("correction_of"):
            return "correction_existing_sources_only"
        policy = scope.get("task_brief") or {}
        calls = scope.setdefault("source_calls", {})
        if call["id"] in calls:
            if calls[call["id"]]["identity"] != identity:
                raise ValueError("Source call identity changed during replay")
            return None
        group_id = state.dispatches.get(dispatch.dispatch_id, {}).get("work_group")
        group = None
        if group_id:
            group = state.role_data.setdefault("collection_work", {}).setdefault(
                group_id,
                {
                    "calls": {},
                    "max_source_calls": policy.get("max_source_calls", 8),
                    "max_no_progress_calls": policy.get("max_no_progress_calls", 2),
                    "no_progress_calls": 0,
                },
            )
            if (
                group.get("stop_reason")
                or group["no_progress_calls"] >= group["max_no_progress_calls"]
            ):
                return "no_new_evidence"
            if len(group["calls"]) >= group["max_source_calls"]:
                return "source_call_limit"
        if len(calls) >= policy.get("max_source_calls", 8):
            return "source_call_limit"
        if scope.get("no_progress_calls", 0) >= policy.get("max_no_progress_calls", 2):
            return "no_new_evidence"
        if group is not None:
            group["calls"][dispatch.dispatch_id + ":" + call["id"]] = identity
        calls[call["id"]] = {
            "identity": identity,
            "status": "started",
        }
        return None

    return await services.repository.mutate_control(context, reserve)


async def finish_source_call(services, context, dispatch, call, result=None):
    def finish(state):
        scope = state.role_data["scopes"][dispatch.dispatch_id]
        record = scope["source_calls"][call["id"]]
        if record["status"] == "completed":
            return
        # Progress comes from this call's own new evidence receipts. Counting the
        # global evidence list would let a concurrent dispatch mask a failed query.
        progressed = False
        if result is not None:
            import json

            try:
                payload = json.loads(result.content)
            except (ValueError, TypeError):
                payload = {}
            if isinstance(payload, dict):
                added = payload.get("new_evidence_ids")
                if isinstance(added, list):
                    record["new_evidence_ids"] = list(added)
                    progressed = bool(added)
        scope["no_progress_calls"] = (
            0 if progressed else scope.get("no_progress_calls", 0) + 1
        )
        group_id = state.dispatches.get(dispatch.dispatch_id, {}).get("work_group")
        group = state.role_data.get("collection_work", {}).get(group_id)
        if group is not None:
            group["no_progress_calls"] = (
                0 if progressed else group["no_progress_calls"] + 1
            )
        record.update(status="completed", new_evidence=progressed)

    await services.repository.mutate_control(context, finish)
