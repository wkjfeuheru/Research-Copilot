"""Bounded Docker CLI transport. Shell commands are arguments inside the container only.

https://docs.docker.com/reference/cli/docker/container/run/
The container is paused before copying tmpfs, killed before any artifact is imported.
"""

import asyncio
from dataclasses import dataclass
import io
import json
import os
import re
from pathlib import Path
import socket
import subprocess
import tempfile
import shutil
import tarfile
import time

from .config import SandboxConfig
from .contracts import relative_path
from research_copilot.workspace.filesystem import reject_links

LABEL = "research-copilot.analysist-sandbox"


def process_alive(pid):
    if os.name == "nt":
        # os.kill(pid, 0) terminates processes on Windows; query a handle instead.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            return (
                bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
                and exit_code.value == 259
            )
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class DockerUnavailable(RuntimeError):
    pass


class ExecutionUnknown(RuntimeError):
    """Do not automatically replay an external execution whose outcome is unknown."""


@dataclass(frozen=True)
class RunOutput:
    exit_code: int | None
    output: str
    truncated: bool
    archive: bytes
    status: str
    seconds: float
    image_digest: str


def import_archive(raw, destination, config):
    """Extract manually, rejecting every link/device/alias before creating a file."""
    if len(raw) > config.output_bytes + config.output_files * 2048 + 10240:
        raise ValueError("Output archive exceeds limit")
    files, total, seen = [], 0, set()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) > config.output_files * 3 + 10:
            raise ValueError("Too many archive entries")
        for member in members:
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            if member.isdir() and name in {"", "."}:
                continue
            name = relative_path(name.rstrip("/") if member.isdir() else name)
            if member.isdir():
                continue
            if not member.isfile() or member.issym() or member.islnk():
                raise ValueError("Only regular files; no links")
            if name in seen:
                raise ValueError("Duplicate archive output")
            seen.add(name)
            total += member.size
            if (
                len(seen) > config.output_files
                or member.size < 0
                or total > config.output_bytes
            ):
                raise ValueError("Output size/count limit exceeded")
            stream = archive.extractfile(member)
            content = stream.read(member.size + 1)
            if len(content) != member.size:
                raise ValueError("Truncated output archive")
            files.append((name, content))
    for name, content in files:
        path = reject_links(Path(destination) / name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as target:
            target.write(content)
    return {name: content for name, content in files}


class DockerRunner:
    def __init__(self, config=None):
        self.config = config or SandboxConfig()
        self.owner = f"{socket.gethostname()}:{os.getpid()}"
        self._active = {}

    async def _command(self, *args, limit=None, timeout=20):
        try:
            process = subprocess.Popen(
                ["docker", *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            raise DockerUnavailable(
                "Docker executable is unavailable; host execution is forbidden"
            ) from exc
        limit = limit or self.config.log_bytes

        def drain(stream):
            kept = bytearray()
            total = 0
            while chunk := stream.read(16384):
                total += len(chunk)
                kept.extend(chunk[: max(0, limit - len(kept))])
            return bytes(kept), total > limit

        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await asyncio.gather(
                    asyncio.to_thread(drain, process.stdout),
                    asyncio.to_thread(drain, process.stderr),
                )
                code = await asyncio.to_thread(process.wait)
        except BaseException:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait)
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
        return code, stdout[0], stderr[0], stdout[1] or stderr[1]

    async def image_identity(self):
        code, out, err, _ = await self._command(
            "image", "inspect", self.config.image, "--format", "{{.Id}}"
        )
        image = out.decode().strip()
        if code or not image.startswith("sha256:"):
            raise DockerUnavailable(
                "Docker or prebuilt sandbox image is unavailable; no pull/host fallback"
            )
        if self.config.image_digest and self.config.image_digest != image:
            raise ValueError("Sandbox image digest differs from approved configuration")
        return image

    async def cleanup_orphans(self):
        """Only reclaim this project's containers owned by dead local application processes."""
        code, out, _, _ = await self._command(
            "ps",
            "-a",
            "--filter",
            f"label={LABEL}=1",
            "--format",
            '{{.ID}} {{.Label "rc.owner"}}',
        )
        if code:
            raise DockerUnavailable("Cannot discover orphan sandbox containers")
        removed = []
        for line in out.decode().splitlines():
            ident, _, owner = line.partition(" ")
            host, _, pid = owner.rpartition(":")
            if host != socket.gethostname() or not pid.isdigit():
                continue
            if not process_alive(int(pid)):
                await self._command("rm", "-f", ident)
                removed.append(ident)
        code, out, _, _ = await self._command(
            "volume", "ls", "--filter", f"label={LABEL}=1", "--format", "{{.Name}}"
        )
        if code:
            raise DockerUnavailable("Cannot discover orphan sandbox volumes")
        for volume in out.decode().splitlines():
            code, raw, _, _ = await self._command(
                "volume", "inspect", volume, "--format", '{{index .Labels "rc.owner"}}'
            )
            host, _, pid = raw.decode().strip().rpartition(":")
            if code or host != socket.gethostname() or not pid.isdigit():
                continue
            if not process_alive(int(pid)):
                await self._command("volume", "rm", volume)
        return removed

    async def cancel_report(self, report_id):
        for name in tuple(self._active.get(report_id, set())):
            await self._command("kill", name)

    async def confirm_cleanup(self, execution_id):
        """Require successful daemon listings; an inspect error is not absence.

        Docker name filters may match substrings; any matching resource blocks
        release, including stopped containers. Never delete resources here.
        """
        if not re.fullmatch(r"exec-[a-f0-9]{32}", execution_id):
            raise ValueError("Invalid sandbox execution identity")
        name = "rc-analysis-" + execution_id.removeprefix("exec-")
        for command in (
            ("ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"),
            (
                "volume",
                "ls",
                "--filter",
                f"name={name}-outputs",
                "--format",
                "{{.Name}}",
            ),
        ):
            code, out, _, over = await self._command(*command)
            if code or over or out.strip():
                raise ExecutionUnknown(
                    "Sandbox resources are present or cleanup cannot be confirmed"
                )

    async def run(
        self,
        *,
        execution_id,
        report_id,
        dispatch_id,
        snapshot,
        command,
        control,
        timeout=None,
        image_digest=None,
    ):
        # Docker Desktop's Windows file share cannot open some deeply nested paths.
        # A short immutable staging copy also avoids mounting live report directories.
        from research_copilot.workspace.filesystem import tree_files

        with tempfile.TemporaryDirectory(prefix="rcx-") as temporary:
            stage = Path(temporary)
            (stage / "workspace/tasks" / dispatch_id).mkdir(parents=True)
            (stage / "skills").mkdir()
            for source in tree_files(Path(snapshot)):
                target = stage / source.relative_to(snapshot)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            volume = "rc-analysis-" + execution_id.removeprefix("exec-") + "-outputs"
            code, _, err, _ = await self._command(
                "volume",
                "create",
                "--driver",
                "local",
                "--opt",
                "type=tmpfs",
                "--opt",
                "device=tmpfs",
                "--opt",
                f"o=size={self.config.output_bytes},mode=1777,noexec,nosuid,nodev",
                "--label",
                f"{LABEL}=1",
                "--label",
                f"rc.owner={self.owner}",
                volume,
            )
            if code:
                raise DockerUnavailable(
                    "Cannot create bounded output volume: "
                    + err.decode(errors="replace")
                )
            try:
                return await self._run(
                    execution_id=execution_id,
                    report_id=report_id,
                    dispatch_id=dispatch_id,
                    snapshot=stage,
                    command=command,
                    control=control,
                    timeout=timeout,
                    image_digest=image_digest,
                    output_volume=volume,
                )
            finally:
                cleanup = asyncio.create_task(self._command("volume", "rm", volume))
                code, _, err, _ = await asyncio.shield(cleanup)
                if code:
                    raise ExecutionUnknown(
                        "Cannot reclaim output volume: " + err.decode(errors="replace")
                    )

    async def _run(
        self,
        *,
        execution_id,
        report_id,
        dispatch_id,
        snapshot,
        command,
        control,
        timeout=None,
        image_digest=None,
        output_volume,
    ):
        config = self.config
        image = image_digest or await self.image_identity()
        name = "rc-analysis-" + execution_id.removeprefix("exec-")
        snapshot = reject_links(Path(snapshot)).resolve()
        started = time.monotonic()
        log = b""
        truncated = False
        archive = b""
        status = "failed"
        exit_code = None
        # Each mount is an immutable per-execution copy, never the report/repository.
        args = [
            "create",
            "--name",
            name,
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "10001:10001",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(config.pids),
            "--cpus",
            str(config.cpus),
            "--memory",
            f"{config.memory_mib}m",
            "--memory-swap",
            f"{config.memory_mib}m",
            "--ipc",
            "private",
            "--log-driver",
            "none",
            "--no-healthcheck",
            "--label",
            f"{LABEL}=1",
            "--label",
            f"rc.owner={self.owner}",
            "--label",
            f"rc.report={report_id}",
            "--label",
            f"rc.execution={execution_id}",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={config.temporary_mib}m,mode=1777",
            "--mount",
            f"type=volume,src={output_volume},dst=/outputs,volume-nocopy",
            "--env",
            "RC_OUTPUT_DIR=/outputs",
            "--env",
            "MPLCONFIGDIR=/tmp/matplotlib",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONPATH=/skills/analysist/financial-modeling/scripts",
            "--workdir",
            f"/workspace/tasks/{dispatch_id}",
        ]
        for directory in ("workspace", "skills"):
            root = snapshot / directory
            args.extend(["--mount", f"type=bind,src={root},dst=/{directory},readonly"])
        args.extend([image, "sleep", "infinity"])
        await control.acheck()
        code, _, err, _ = await self._command(*args)
        if code:
            raise DockerUnavailable(
                "Cannot create sandbox: " + err.decode(errors="replace")
            )
        self._active.setdefault(report_id, set()).add(name)
        try:
            code, _, err, _ = await self._command("start", name)
            if code:
                raise ExecutionUnknown(
                    "Sandbox start failed: " + err.decode(errors="replace")
                )
            execute = asyncio.create_task(
                self._command(
                    "exec",
                    name,
                    "/bin/sh",
                    "-lc",
                    command,
                    timeout=config.effective_timeout(timeout) + 5,
                )
            )
            try:
                deadline = time.monotonic() + config.effective_timeout(timeout)
                while not execute.done():
                    await control.acheck()
                    if time.monotonic() >= deadline:
                        status = "timeout"
                        break
                    await asyncio.wait({execute}, timeout=0.1)
                if status == "timeout":
                    await self._command("kill", name)
                exit_code, out, err, truncated = await execute
                log = (out + b"\n" + err)[: config.log_bytes]
                truncated = truncated or len(out) + len(err) > config.log_bytes
                if status != "timeout":
                    state_code, state_raw, _, _ = await self._command(
                        "inspect", name, "--format", "{{json .State}}"
                    )
                    if state_code:
                        raise ExecutionUnknown("Cannot inspect sandbox outcome")
                    state = json.loads(state_raw)
                    status = (
                        "oom"
                        if state.get("OOMKilled")
                        else "completed"
                        if exit_code == 0 and state.get("Running")
                        else "failed"
                    )
                    if status == "completed":
                        code, _, _, _ = await self._command("pause", name)
                        if code:
                            raise ExecutionUnknown("Cannot freeze sandbox output")
                        code, archive, err, over = await self._command(
                            "cp",
                            name + ":/outputs/.",
                            "-",
                            limit=config.output_bytes
                            + config.output_files * 2048
                            + 10240,
                        )
                        if code or over:
                            raise ValueError("Cannot collect bounded sandbox output")
            except BaseException as exc:
                # Drain partial logs after stopping the container, including cancellation.
                if not execute.done():
                    await asyncio.shield(self._command("kill", name))
                try:
                    _, out, err, truncated = await asyncio.shield(execute)
                    log = (out + b"\n" + err)[: config.log_bytes]
                    truncated = truncated or len(out) + len(err) > config.log_bytes
                except BaseException:
                    pass
                exc.sandbox_output = log.decode("utf-8", errors="replace")
                exc.sandbox_truncated = truncated
                raise
            finally:
                if not execute.done():
                    execute.cancel()
                    await asyncio.gather(execute, return_exceptions=True)
        finally:
            # rm -f removes all processes including detached descendants, even on cancellation.
            cleanup = asyncio.create_task(self._command("rm", "-f", name))
            code, _, err, _ = await asyncio.shield(cleanup)
            self._active.get(report_id, set()).discard(name)
            if code:
                raise ExecutionUnknown(
                    "Cannot confirm container destruction: "
                    + err.decode(errors="replace")
                )
        return RunOutput(
            exit_code,
            log.decode("utf-8", errors="replace"),
            truncated,
            archive,
            status,
            time.monotonic() - started,
            image,
        )
