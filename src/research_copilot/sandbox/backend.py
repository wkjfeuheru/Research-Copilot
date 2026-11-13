"""Complete Deep Agents 0.7.32 sandbox protocol with controlled file delegation.

https://docs.langchain.com/oss/python/deepagents/backends#implement-a-custom-backend
"""

from contextvars import ContextVar
import asyncio
import json
from uuid import uuid4
from deepagents.backends.protocol import SandboxBackendProtocol, ExecuteResponse

execution_call = ContextVar("execution_call", default=None)


class AnalysisSandboxBackend(SandboxBackendProtocol):
    def __init__(self, file_backend, execution_service):
        self.files = file_backend
        self.execution_service = execution_service
        self._id = "analysist-docker-" + uuid4().hex
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    @property
    def id(self):
        return self._id

    def ls(self, *args, **kwargs):
        return self.files.ls(*args, **kwargs)

    def read(self, *args, **kwargs):
        return self.files.read(*args, **kwargs)

    def grep(self, *args, **kwargs):
        return self.files.grep(*args, **kwargs)

    def glob(self, *args, **kwargs):
        return self.files.glob(*args, **kwargs)

    def write(self, *args, **kwargs):
        return self.files.write(*args, **kwargs)

    def edit(self, *args, **kwargs):
        return self.files.edit(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return self.files.delete(*args, **kwargs)

    def upload_files(self, *args, **kwargs):
        return self.files.upload_files(*args, **kwargs)

    def download_files(self, *args, **kwargs):
        return self.files.download_files(*args, **kwargs)

    async def als(self, *args, **kwargs):
        return await self.files.als(*args, **kwargs)

    async def aread(self, *args, **kwargs):
        return await self.files.aread(*args, **kwargs)

    async def agrep(self, *args, **kwargs):
        return await self.files.agrep(*args, **kwargs)

    async def aglob(self, *args, **kwargs):
        return await self.files.aglob(*args, **kwargs)

    async def awrite(self, *args, **kwargs):
        return await self.files.awrite(*args, **kwargs)

    async def aedit(self, *args, **kwargs):
        return await self.files.aedit(*args, **kwargs)

    async def adelete(self, *args, **kwargs):
        return await self.files.adelete(*args, **kwargs)

    async def aupload_files(self, *args, **kwargs):
        return await self.files.aupload_files(*args, **kwargs)

    async def adownload_files(self, *args, **kwargs):
        return await self.files.adownload_files(*args, **kwargs)

    def execute(self, command, *, timeout=None):
        # Sync native tools run in worker threads; reuse the authenticated service's loop.
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if self.loop is None or not self.loop.is_running() or current is self.loop:
            raise RuntimeError(
                "Sync execution requires a worker thread and the bound runtime loop; use aexecute"
            )
        future = asyncio.run_coroutine_threadsafe(
            self.aexecute(command, timeout=timeout), self.loop
        )
        return future.result()

    async def aexecute(self, command, *, timeout=None):
        runtime = execution_call.get()
        if runtime is None:
            raise PermissionError("Missing trusted execution tool context")
        response = await self.execution_service.execute(
            runtime.context, runtime.tool_call_id, command, timeout
        )
        return ExecuteResponse(
            output=json.dumps(response, ensure_ascii=False),
            exit_code=response.get("exit_code"),
            truncated=response.get("truncated", False),
        )
