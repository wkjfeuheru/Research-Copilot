"""Authenticated host-side input import; never exposed as an agent file tool."""

import asyncio
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from . import safe_component
from .filesystem import reject_links

from research_copilot.runtime.identity import RUNTIME_VERSION
from research_copilot.runtime.task_completion import await_completion


@dataclass(frozen=True, slots=True)
class InputFile:
    report_id: str
    path: str
    content_hash: str
    byte_size: int


def _filename(value: str) -> str:
    if (
        not value
        or len(value) > 180
        or value.endswith((".", " "))
        or any(c in value for c in '/\\:<>"|?*')
        or any(ord(c) < 32 for c in value)
        or value in {".", ".."}
        or re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value.split(".")[0], re.I)
    ):
        raise ValueError("Invalid input filename")
    return value


class WorkspaceFiles:
    """The host supplies an authenticated actor; the store checks report ownership.

    Serialize with the same report lock as runs. Imports are immutable by hash and
    never register an evidence_id or submit a formal business artifact.
    """

    def __init__(
        self,
        *,
        store,
        workspace_root: Path,
        policy_version: str,
        max_bytes=10 * 1024 * 1024,
    ):
        self.store, self.workspace_root = store, workspace_root
        self.policy_version, self.max_bytes = policy_version, max_bytes

    async def _authorize(self, report_id, actor_id):
        safe_component(report_id)
        record = await self.store.load(report_id, actor_id)
        if record.report_id != report_id or record.actor_id != actor_id:
            raise PermissionError("Session identity mismatch")
        if (
            record.runtime_version != RUNTIME_VERSION
            or record.policy_version != self.policy_version
        ):
            raise ValueError("Input requires the current policy")
        return reject_links(self.workspace_root / report_id)

    async def import_input(
        self, report_id: str, actor_id: str, filename: str, content: bytes
    ) -> InputFile:
        filename = _filename(filename)
        if not isinstance(content, bytes) or len(content) > self.max_bytes:
            raise ValueError("Input exceeds the configured byte limit or is not bytes")
        digest = hashlib.sha256(content).hexdigest()
        async with self.store.lock(report_id):
            root = await self._authorize(report_id, actor_id)
            relative = f"inputs/{digest}/{filename}"
            target = reject_links(root / relative)

            def write():
                target.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation preserves old inputs on replay/concurrent import.
                try:
                    with target.open("xb") as stream:
                        stream.write(content)
                except FileExistsError:
                    if target.read_bytes() != content:
                        raise ValueError("Existing input hash mismatch")

            await await_completion(asyncio.to_thread(write))
        return InputFile(report_id, f"/workspace/{relative}", digest, len(content))

    async def read_input(
        self, report_id: str, actor_id: str, reference: InputFile
    ) -> bytes:
        async with self.store.lock(report_id):
            root = await self._authorize(report_id, actor_id)
            prefix = f"/workspace/inputs/{reference.content_hash}/"
            if (
                reference.report_id != report_id
                or not re.fullmatch(r"[a-f0-9]{64}", reference.content_hash)
                or not reference.path.startswith(prefix)
            ):
                raise PermissionError("Input reference belongs to another scope")
            filename = _filename(reference.path.removeprefix(prefix))
            target = reject_links(root / "inputs" / reference.content_hash / filename)
            content = await await_completion(asyncio.to_thread(target.read_bytes))
            if (
                len(content) != reference.byte_size
                or hashlib.sha256(content).hexdigest() != reference.content_hash
            ):
                raise ValueError("Input content hash mismatch")
            return content
