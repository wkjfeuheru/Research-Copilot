"""SSE from durable workbench state and committed documents; never runs a model.

https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events
"""

import asyncio
import hashlib
import json
import re
import time

from research_copilot.contracts import RunContext
from research_copilot.web.workspace import build_workspace, select_report


def frame(event, data, ident=None):
    return (
        (f"id: {ident}\n" if ident is not None else "")
        + f"event: {event}\ndata: "
        + json.dumps(data, ensure_ascii=False, allow_nan=False)
        + "\n\n"
    )


async def workspace_events(
    backend, report_id, disconnected, *, last_event_id="", poll_interval=0.8
):
    signature = None
    sent_report = None
    heartbeat = time.monotonic()
    cursor = re.fullmatch(r"report:([a-f0-9]{64}):(\d{1,9})", last_event_id or "")
    while not await disconnected():
        try:
            snapshot = await build_workspace(backend, report_id)
            encoded = json.dumps(
                snapshot, ensure_ascii=False, sort_keys=True, default=str
            )
            current = hashlib.sha256(encoded.encode()).hexdigest()
            if current != signature:
                yield frame("workspace", snapshot)
                signature = current
            record = await backend.store.load(report_id, backend.actor_id)
            context = RunContext(
                record.actor_id,
                record.report_id,
                record.run_id,
                record.budget_id,
                record.policy_version,
            )
            state = await backend.store.read_control(context)
            document = select_report(state, None)
            if document:
                text = document["document"]
                digest = hashlib.sha256(text.encode()).hexdigest()
                identity = (document["artifact_id"], document["version"], digest)
                if identity != sent_report:
                    offset = (
                        int(cursor[2])
                        if cursor
                        and cursor[1] == digest
                        and int(cursor[2]) <= len(text)
                        else 0
                    )
                    metadata = {
                        "version": document["version"],
                        "content_hash": digest,
                        "frozen": document["frozen"],
                        "offset": offset,
                    }
                    yield frame("report.start", metadata)
                    for start in range(offset, len(text), 1024):
                        if await disconnected():
                            return
                        chunk = text[start : start + 1024]
                        yield frame(
                            "report.delta",
                            {"content_hash": digest, "offset": start, "text": chunk},
                            f"report:{digest}:{start + len(chunk)}",
                        )
                    yield frame(
                        "report.complete",
                        {**metadata, "length": len(text)},
                        f"report:{digest}:{len(text)}",
                    )
                    sent_report = identity
                    cursor = None
            elif sent_report:
                yield frame("report.reset", {}, "")
                sent_report = None
            if time.monotonic() - heartbeat >= 10:
                yield ": heartbeat\n\n"
                heartbeat = time.monotonic()
        except LookupError:
            yield frame("gone", {"message": "对话已删除或不可访问。"})
            return
        except (PermissionError, ValueError):
            yield frame(
                "stream.error", {"message": "无法读取当前研究状态，请重新连接。"}
            )
            return
        await asyncio.sleep(poll_interval)
