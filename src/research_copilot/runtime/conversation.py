"""Durable user-facing conversation. Internal tools/reasoning never become chat text."""

import hashlib
from datetime import UTC, datetime


def append_message(state, *, message_id, role, content, kind="text"):
    messages = state.role_data.setdefault("conversation", [])
    existing = next((m for m in messages if m["id"] == message_id), None)
    if existing:
        if existing["role"] != role or existing["content"] != content:
            raise ValueError("Message identity was reused with different content")
        return existing
    message = {
        "id": message_id,
        "role": role,
        "content": content,
        "kind": kind,
        "created_at": datetime.now(UTC).isoformat(),
    }
    messages.append(message)
    return message


def assistant_messages(result):
    from langchain_core.messages import AIMessage

    for item in result.get("messages", []):
        if not isinstance(item, AIMessage) or item.tool_calls:
            continue
        content = item.content
        if isinstance(content, list):
            content = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if isinstance(content, str) and content.strip():
            ident = item.id or hashlib.sha256(content.encode()).hexdigest()
            yield (
                "assistant:" + ident,
                content,
                bool(item.additional_kwargs.get("awaiting_user_input")),
            )


def public_error(exc):
    code = type(exc).__name__
    if code == "WorkflowBlocked":
        reason = str(exc)
        if "Planner returned no plan proposal" in reason:
            return "规划回复未形成计划。请补充研究期间或范围后继续对话，已有预算和检查点会保留。"
        return "研究暂时受阻：" + reason[:600]
    if code == "SandboxQueueTimeout":
        return "沙盒排队超时，本次计算尚未启动。请在有空闲执行槽时继续。"
    if code == "BudgetExceeded":
        details = getattr(exc, "details", {})
        role = {
            "supervisor": "规划",
            "searcher": "采集",
            "analysist": "分析",
            "writer": "写作",
            "reviewer": "审核",
        }.get(details.get("stage"))
        keys = (
            "requested_reservation",
            "stage_remaining_tokens",
            "unknown_reserved_tokens",
        )
        if role and all(type(details.get(k)) is int and details[k] >= 0 for k in keys):
            return (
                f"{role}阶段预算不足：下次请求需预留 {details[keys[0]]:,} token，"
                f"阶段剩余 {details[keys[1]]:,} token；用量未知的历史请求占额 "
                f"{details[keys[2]]:,} token。总预算有余额也不能突破阶段上限。"
                "请先核实未知用量或明确调整阶段预算；重复继续不会解决，已有来源与累计用量保留。"
            )
        return "预算不足以预留下一次请求或已达到调用上限，请查看阶段及总预算记录后决定下一步。"
    if code in {"SessionBusy", "RunCancelled"}:
        return "研究正在运行或已取消，请刷新当前状态。"
    return "运行未完成（" + code + "），请查看当前进度和可恢复状态。"
