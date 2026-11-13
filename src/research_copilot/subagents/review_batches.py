"""Bounded frozen text pages. Only fully delivered targets count as read."""

import hashlib
from research_copilot.subagents.services import digest


async def read_batch(service, context, snapshot, branch, target_ids, cursor=None):
    if not 1 <= len(target_ids) <= 12 or len(set(target_ids)) != len(target_ids):
        raise ValueError("Batch requires 1-12 unique targets")
    content = await service.load_snapshot(context, snapshot)
    required = content[branch + "_targets"]
    targets = []
    for ident in target_ids:
        target = content["targets"].get(ident)
        if ident not in required or target is None:
            raise PermissionError("Target outside branch snapshot")
        if "image_base64" in target:
            raise ValueError("Images require the single image read tool")
        if hashlib.sha256(target["text"].encode()).hexdigest() != target["hash"]:
            raise ValueError("Review target hash mismatch")
        targets.append(target)
    key = digest(
        [snapshot["snapshot_id"], branch, target_ids, [t["hash"] for t in targets]]
    )
    state = await service.services.repository.read_control(context)
    pages = state.role_data.get("review_pages", {}).get(key, {})
    index, offset = 0, 0
    if cursor:
        if cursor not in pages:
            raise ValueError("Unknown or mismatched review cursor")
        index, offset = pages[cursor]
    items, completed, remaining = [], [], 48 * 1024
    while index < len(targets) and remaining:
        target = targets[index]
        encoded = target["text"][offset:].encode()
        text = encoded[:remaining].decode("utf-8", errors="ignore")
        if not text and encoded:
            break
        end = offset + len(text)
        complete = end == len(target["text"])
        items.append(
            {
                "target_id": target_ids[index],
                "content": text,
                "content_hash": target["hash"],
                "start": offset,
                "end": end,
                "complete": complete,
            }
        )
        remaining -= len(text.encode())
        if not complete:
            offset = end
            break
        completed.append(target_ids[index])
        index, offset = index + 1, 0
    next_cursor = digest([key, index, offset]) if index < len(targets) else None

    def record(state):
        if next_cursor:
            state.role_data.setdefault("review_pages", {}).setdefault(key, {})[
                next_cursor
            ] = [index, offset]
        reads = (
            state.role_data.setdefault("review_reads", {})
            .setdefault(snapshot["snapshot_id"], {})
            .setdefault(branch, [])
        )
        for ident in completed:
            if ident not in reads:
                reads.append(ident)

    await service.services.repository.mutate_control(context, record)
    return {"items": items, "next_cursor": next_cursor}
