"""MCP 客户端：单一会话所有者任务 + 串行请求队列。

支持两种传输：

- ``stdio``：本地子进程（CNEquity）；
- ``http``：MCP Streamable HTTP（TickDB 托管服务），认证头只保存在配置对象里，
  不进入日志、错误消息或证据。

为什么需要所有者任务（m2-design 5.1）：

- ``stdio_client`` / ``streamablehttp_client`` / ``ClientSession`` 是 AnyIO context；
  资源必须在同一个任务里创建和关闭，不能在工作流节点任务里打开、在 CLI 另一个任务里关闭。
- 所有 list/call 请求进入同一个队列，由所有者串行执行，避免并发调用同一会话。
- 取消调用时通知所有者取消当前请求并重建会话，不把 context 的 ``__aexit__`` 交给
  任意任务。
- 子进程/连接异常退出后至多重建一次；第二次失败返回安全工具错误，不无限重启。

本模块只负责传输与会话，不知道 CNEquity/TickDB 的业务语义，也不写数据库。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Mapping

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

try:  # mcp >= 1.23 推荐新入口；旧版本回退到 streamablehttp_client。
    from mcp.client.streamable_http import streamable_http_client
except ImportError:  # pragma: no cover - 由依赖下限保证
    streamable_http_client = None  # type: ignore[assignment]
from mcp.shared.exceptions import McpError

from research_copilot.sources.fingerprint import schema_fingerprint
from research_copilot.runtime.source_audit import span

logger = logging.getLogger(__name__)


DEFAULT_INIT_TIMEOUT_SECONDS = 20.0
DEFAULT_CALL_TIMEOUT_SECONDS = 90.0
DEFAULT_CLOSE_TIMEOUT_SECONDS = 10.0
MAX_LIST_PAGES = 50


class MCPClientError(RuntimeError):
    """MCP 客户端失败；消息面向用户，不含参数、认证头或原始异常文本。

    ``code`` 是稳定的安全分类，供诊断和按域降级使用，不依赖异常文本。
    """

    code = "mcp_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class MCPUnavailable(MCPClientError):
    """会话不可用（启动失败、子进程退出、重建额度用尽）。"""

    code = "unavailable"


class MCPTimeout(MCPClientError):
    """初始化、调用或退出超时。"""

    code = "timeout"


class MCPProtocolError(MCPClientError):
    """协议错误、无效返回结构或工具执行异常。"""

    code = "protocol_error"


class MCPCancelled(MCPClientError):
    """调用被取消。"""

    code = "cancelled"


MCPTransport = Literal["stdio", "http"]


@dataclass(frozen=True, slots=True)
class MCPServerParameters:
    """MCP 服务连接参数。

    ``stdio`` 用参数数组启动子进程，禁止 shell 拼接；``http`` 连接 Streamable HTTP
    端点，``headers`` 可携带服务方要求的认证头。``headers``/``env`` 不进 repr，
    避免凭证出现在异常或调试输出中。
    """

    transport: MCPTransport = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: Mapping[str, str] | None = field(default=None, repr=False)
    url: str | None = None
    headers: Mapping[str, str] | None = field(default=None, repr=False)
    #: Custom CA bundle for HTTPS sources. Certificate and hostname checks stay on.
    ca_bundle: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.transport == "stdio":
            if not self.command.strip():
                raise ValueError("stdio 传输必须提供启动命令")
            return
        if self.transport == "http":
            if not self.url or not self.url.strip():
                raise ValueError("http 传输必须提供 MCP 服务 URL")
            if not self.url.strip().startswith(("http://", "https://")):
                raise ValueError("MCP 服务 URL 必须以 http:// 或 https:// 开头")
            if self.ca_bundle is not None and (
                not isinstance(self.ca_bundle, str) or not self.ca_bundle.strip()
            ):
                raise ValueError("MCP CA bundle 必须是非空文件路径")
            return
        raise ValueError(f"未知的 MCP 传输方式：{self.transport}")

    @property
    def safe_description(self) -> str:
        """可展示的传输描述；只包含 URL/命令，不包含认证头或环境变量。"""

        if self.transport == "http":
            return f"HTTP {self.url}"
        return " ".join((self.command, *self.args)).strip()


@dataclass(frozen=True, slots=True)
class MCPToolInfo:
    """一个已发现工具的 schema 与指纹。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    read_only_hint: bool | None
    schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class MCPToolResult:
    """一次工具调用的结构化结果；由上层决定如何转成证据。"""

    is_error: bool
    structured: Any | None
    text_blocks: tuple[str, ...]
    unsupported_blocks: tuple[str, ...]


@dataclass(slots=True)
class _Request:
    operation: Callable[[ClientSession], Awaitable[Any]]
    future: asyncio.Future = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    #: 调用方已取消/超时；即使底层调用返回，也必须重建会话并丢弃结果。
    aborted: bool = False

    def resolve(self, value: Any) -> None:
        if not self.future.done():
            self.future.set_result(value)

    def fail(self, error: BaseException) -> None:
        if not self.future.done():
            self.future.set_exception(error)


_STOP = object()


class MCPClient:
    """一个 MCP stdio 会话的所有者与请求代理。"""

    def __init__(
        self,
        parameters: MCPServerParameters,
        *,
        init_timeout: float = DEFAULT_INIT_TIMEOUT_SECONDS,
        call_timeout: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        close_timeout: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
        max_crash_restarts: int = 1,
        name: str = "mcp",
        result_filter: Callable[[MCPToolResult], MCPToolResult] | None = None,
    ) -> None:
        if max_crash_restarts < 0:
            raise ValueError("max_crash_restarts 不能为负数")
        self._parameters = parameters
        self._init_timeout = init_timeout
        self._call_timeout = call_timeout
        self._close_timeout = close_timeout
        self._max_crash_restarts = max_crash_restarts
        self._name = name
        self._result_filter = result_filter
        self._verify = _verify_context(parameters.ca_bundle)

        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._owner_task: asyncio.Task[None] | None = None
        self._owner_error: Exception | None = None
        self._server_info: dict[str, Any] = {}
        self._generation = 0
        self._crash_restarts = 0
        self._closed = False
        self._current_request: _Request | None = None
        self._current_operation: asyncio.Task[Any] | None = None
        self._session: ClientSession | None = None

    # --- 生命周期 -------------------------------------------------------

    @property
    def generation(self) -> int:
        """会话代数；每次重建 +1，供上层重新核对 schema。"""

        return self._generation

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    @property
    def crash_restarts(self) -> int:
        return self._crash_restarts

    async def start(self) -> None:
        """创建所有者任务并等待初始化；重复调用幂等。"""

        if self._closed:
            raise MCPUnavailable("MCP 会话已关闭")
        if self._owner_task is not None:
            if self._owner_error is not None:
                raise self._safe_owner_error()
            return

        self._ready = asyncio.Event()
        self._owner_task = asyncio.create_task(
            self._owner_loop(), name=f"{self._name}-session-owner"
        )
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._init_timeout + 5)
        except TimeoutError:
            await self.aclose()
            raise MCPTimeout(f"MCP 会话初始化超时；{self._transport_hint}") from None
        if self._owner_error is not None:
            await self.aclose()
            raise self._safe_owner_error()

    async def aclose(self) -> None:
        """要求所有者关闭会话并验证退出；可重复调用。"""

        owner = self._owner_task
        if owner is None:
            self._closed = True
            return
        self._closed = True
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_STOP)
        try:
            await asyncio.wait_for(asyncio.shield(owner), timeout=self._close_timeout)
        except TimeoutError:
            logger.warning("MCP 会话关闭超时，取消所有者任务")
            owner.cancel()
            with contextlib.suppress(BaseException):
                await owner
        except asyncio.CancelledError:
            owner.cancel()
            with contextlib.suppress(BaseException):
                await owner
            raise
        finally:
            self._owner_task = None
            self._session = None
            self._fail_pending(MCPUnavailable("MCP 会话已关闭"))

    async def __aenter__(self) -> MCPClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    # --- 公开操作 -------------------------------------------------------

    async def list_tools(self) -> tuple[MCPToolInfo, ...]:
        """遍历分页发现工具；按 cursor 防循环。"""

        await self.start()
        tools: list[MCPToolInfo] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for _page in range(MAX_LIST_PAGES):

            async def read_page(session: ClientSession, page_cursor=cursor):
                return await session.list_tools(cursor=page_cursor)

            result = await self._request(read_page)
            tools.extend(
                _tool_info(tool) for tool in (getattr(result, "tools", None) or ())
            )
            next_cursor = getattr(result, "nextCursor", None)
            if not next_cursor:
                return tuple(tools)
            cursor = str(next_cursor)
            if cursor in seen_cursors:
                raise MCPProtocolError("MCP list_tools 分页游标循环，已中止发现")
            seen_cursors.add(cursor)
        raise MCPProtocolError("MCP list_tools 分页超过上限，已中止发现")

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> MCPToolResult:
        async with span("source", "mcp." + name, source="mcp") as row:
            result = await self._call_tool(name, arguments)
            if self._result_filter:
                result = self._result_filter(result)
            if result.is_error:
                row.update(status="failed", error_type="mcp_tool_error")
            return result

    async def _call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> MCPToolResult:
        """调用一个工具；``isError`` 保留在结果里，不抛异常。"""

        await self.start()
        result = await self._request(
            lambda session: session.call_tool(name, dict(arguments or {}))
        )

        structured = getattr(result, "structuredContent", None)
        is_error = bool(getattr(result, "isError", False))
        text_blocks: list[str] = []
        unsupported: list[str] = []
        for block in getattr(result, "content", None) or ():
            text = getattr(block, "text", None)
            if isinstance(text, str):
                text_blocks.append(text)
            else:
                unsupported.append(type(block).__name__)
        return MCPToolResult(
            is_error=is_error,
            structured=structured,
            text_blocks=tuple(text_blocks),
            unsupported_blocks=tuple(unsupported),
        )

    # --- 所有者任务 -----------------------------------------------------

    async def _owner_loop(self) -> None:
        try:
            while True:
                try:
                    reason = await self._run_session()
                except asyncio.CancelledError:
                    logger.debug("MCP 所有者任务被取消")
                    raise
                except Exception as exc:  # noqa: BLE001 - 初始化失败只暴露安全类别
                    self._owner_error = _safe_start_error(
                        exc, transport=self._parameters.transport
                    )
                    logger.warning("MCP 会话初始化失败：%s", type(exc).__name__)
                    self._fail_pending(self._owner_error)
                    return

                self._session = None
                if reason == "stop":
                    return
                if reason != "restart_crash":
                    # 调用方取消触发的重建不计入"子进程退出"额度。
                    continue
                if self._crash_restarts >= self._max_crash_restarts:
                    self._owner_error = MCPUnavailable(
                        "MCP 服务连接反复中断；本任务不再重建会话"
                    )
                    logger.warning("MCP 会话重建额度用尽")
                    self._fail_pending(self._owner_error)
                    return
                self._crash_restarts += 1
                logger.warning("MCP 会话断开，重建一次")
                continue
        finally:
            self._session = None
            self._ready.set()

    @contextlib.asynccontextmanager
    async def _session_streams(self) -> AsyncIterator[tuple[Any, Any]]:
        """按配置打开 stdio 或 Streamable HTTP 会话流。"""

        if self._parameters.transport == "http":
            # 工具调用可能较慢，SSE 读取不设 httpx read 超时；外层请求仍有
            # ``_call_timeout`` 兜底。认证头只交给本次 httpx 客户端。
            if streamable_http_client is not None:
                http_client = httpx.AsyncClient(
                    headers=dict(self._parameters.headers or {}),
                    verify=self._verify,
                    timeout=httpx.Timeout(
                        max(self._call_timeout, 60.0),
                        connect=self._init_timeout,
                        read=None,
                    ),
                )
                try:
                    async with streamable_http_client(
                        self._parameters.url or "", http_client=http_client
                    ) as streams:
                        yield streams[0], streams[1]
                finally:
                    await http_client.aclose()
                return

            async with streamablehttp_client(  # pragma: no cover - 旧 SDK 回退
                self._parameters.url or "",
                headers=dict(self._parameters.headers or {}),
                timeout=max(self._init_timeout, self._call_timeout),
                sse_read_timeout=max(self._call_timeout, 60.0),
            ) as streams:
                yield streams[0], streams[1]
            return

        params = StdioServerParameters(
            command=self._parameters.command,
            args=list(self._parameters.args),
            cwd=self._parameters.cwd,
            env=dict(self._parameters.env) if self._parameters.env else None,
        )
        async with stdio_client(params) as (read_stream, write_stream):
            yield read_stream, write_stream

    async def _run_session(self) -> str:
        """打开一次会话并服务请求；返回 stop / restart_crash / restart_cancel。"""

        served = False
        reason = "restart_crash"
        try:
            async with self._session_streams() as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    try:
                        init = await asyncio.wait_for(
                            session.initialize(), timeout=self._init_timeout
                        )
                    except TimeoutError as exc:
                        raise MCPTimeout("MCP 会话初始化超时") from exc
                    self._server_info = _server_info(init)
                    self._session = session
                    self._generation += 1
                    self._ready.set()
                    served = True
                    reason = await self._serve(session)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            # 调用方取消后，关闭 AnyIO context 可能再次抛 CancelledError；
            # 所有者任务本身没有被取消，按既定原因重建会话而不是终止所有者。
            if served and reason in {"restart_cancel", "stop"}:
                logger.debug("关闭 MCP 会话时收到取消信号，按原原因继续")
                return reason
            raise
        except BaseException as exc:  # noqa: BLE001 - anyio 退出可能抛 ExceptionGroup
            if not served:
                raise
            if reason == "stop":
                logger.debug("关闭 MCP 会话时上下文报错：%s", type(exc).__name__)
                return "stop"
            logger.warning("MCP 会话上下文异常退出：%s", type(exc).__name__)
        return reason

    async def _serve(self, session: ClientSession) -> str:
        while True:
            request = await self._queue.get()
            if request is _STOP:
                return "stop"
            if not isinstance(request, _Request):  # pragma: no cover - 防御性
                continue
            if request.future.done():
                continue

            async def run_operation():
                return await request.operation(session)

            operation = asyncio.create_task(run_operation())
            self._current_request = request
            self._current_operation = operation
            try:
                result = await operation
            except asyncio.CancelledError:
                current = asyncio.current_task()
                logger.debug(
                    "MCP 操作被取消：owner_cancelling=%s",
                    current.cancelling() if current is not None else -1,
                )
                if current is not None and current.cancelling():
                    raise
                operation.cancel()
                with contextlib.suppress(BaseException):
                    await operation
                request.fail(MCPCancelled("MCP 调用已取消"))
                # 取消可能留下半完成请求；主动重建会话，保证后续调用可用。
                return "restart_cancel"
            except BaseExceptionGroup as exc:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                if request.aborted:
                    return "restart_cancel"
                logger.warning("MCP 调用抛出异常组：%s", type(exc).__name__)
                request.fail(
                    MCPUnavailable(
                        "MCP 会话已断开（异常组）；已尝试重建",
                        code="connection_failed",
                    )
                )
                return "restart_crash"
            except Exception as exc:  # noqa: BLE001 - 会话/协议错误分类
                if request.aborted:
                    return "restart_cancel"
                if _session_broken(exc):
                    request.fail(
                        MCPUnavailable(
                            f"MCP 会话已断开（{type(exc).__name__}）；已尝试重建",
                            code="connection_failed",
                        )
                    )
                    return "restart_crash"
                request.fail(
                    MCPProtocolError(
                        f"MCP 工具调用失败（{type(exc).__name__}）",
                        code="tool_error",
                    )
                )
            else:
                if request.aborted:
                    return "restart_cancel"
                request.resolve(result)
            finally:
                self._current_request = None
                self._current_operation = None

    # --- 请求代理 -------------------------------------------------------

    async def _request(
        self, operation: Callable[[ClientSession], Awaitable[Any]]
    ) -> Any:
        if self._closed:
            raise MCPUnavailable("MCP 会话已关闭")
        if self._owner_error is not None:
            raise self._safe_owner_error()
        request = _Request(operation=operation)
        await self._queue.put(request)
        try:
            return await asyncio.wait_for(request.future, timeout=self._call_timeout)
        except TimeoutError:
            self._abort(request)
            raise MCPTimeout("MCP 工具调用超时") from None
        except asyncio.CancelledError:
            self._abort(request)
            raise

    def _abort(self, request: _Request) -> None:
        request.aborted = True
        request.future.cancel()
        matched = (
            self._current_request is request and self._current_operation is not None
        )
        logger.debug(
            "中止 MCP 请求：current=%s operation=%s",
            self._current_request is request,
            self._current_operation is not None,
        )
        if matched and self._current_operation is not None:
            self._current_operation.cancel()

    def _fail_pending(self, error: BaseException) -> None:
        while True:
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if isinstance(request, _Request):
                request.fail(error)

    @property
    def _transport_hint(self) -> str:
        if self._parameters.transport == "http":
            return "请检查服务地址、API key 与网络"
        return "请检查启动命令与工作目录"

    def _safe_owner_error(self) -> MCPClientError:
        error = self._owner_error
        if isinstance(error, MCPClientError):
            return error
        return MCPUnavailable(f"MCP 会话不可用；{self._transport_hint}")


def _tool_info(tool: Any) -> MCPToolInfo:
    input_schema = getattr(tool, "inputSchema", None)
    output_schema = getattr(tool, "outputSchema", None)
    annotations = getattr(tool, "annotations", None)
    read_only = getattr(annotations, "readOnlyHint", None)
    input_dict = dict(input_schema) if isinstance(input_schema, Mapping) else {}
    output_dict = dict(output_schema) if isinstance(output_schema, Mapping) else None
    return MCPToolInfo(
        name=str(getattr(tool, "name", "")),
        description=str(getattr(tool, "description", "") or ""),
        input_schema=input_dict,
        output_schema=output_dict,
        read_only_hint=bool(read_only) if read_only is not None else None,
        schema_fingerprint=schema_fingerprint(
            {"inputSchema": input_dict, "outputSchema": output_dict}
        )
        or "",
    )


def _server_info(init: Any) -> dict[str, Any]:
    info = getattr(init, "serverInfo", None)
    return {
        "name": str(getattr(info, "name", "") or ""),
        "version": str(getattr(info, "version", "") or ""),
        "protocol_version": str(getattr(init, "protocolVersion", "") or ""),
    }


def _session_broken(exc: BaseException) -> bool:
    """判断异常是否意味着会话/连接已不可用（而不是单次工具错误）。"""

    if isinstance(
        exc,
        (
            anyio.BrokenResourceError,
            anyio.ClosedResourceError,
            anyio.EndOfStream,
            httpx.TransportError,
            ConnectionError,
            EOFError,
            BrokenPipeError,
        ),
    ):
        return True
    if isinstance(exc, McpError):
        text = str(getattr(exc, "error", exc)).lower()
        return any(
            marker in text
            for marker in ("connection closed", "closed", "eof", "broken pipe")
        )
    return False


def _exception_leaves(exc: BaseException) -> list[BaseException]:
    """展开 ExceptionGroup 与 cause 链，供安全分类使用。"""

    leaves: list[BaseException] = []
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        leaves.append(current)
        pending.extend(getattr(current, "exceptions", ()) or ())
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
    return leaves


def _http_status(exc: BaseException) -> int | None:
    for leaf in _exception_leaves(exc):
        response = getattr(leaf, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def _verify_context(ca_bundle: str | None) -> bool | ssl.SSLContext:
    if ca_bundle is None:
        return True
    path = Path(ca_bundle)
    if not path.is_file():
        raise ValueError("MCP CA bundle 文件不存在")
    # create_default_context keeps hostname and certificate verification on;
    # cafile replaces the trust store with the operator-approved bundle.
    return ssl.create_default_context(cafile=str(path))


def _safe_start_error(
    exc: BaseException, *, transport: MCPTransport = "stdio"
) -> MCPClientError:
    if isinstance(exc, MCPClientError):
        return exc
    leaves = _exception_leaves(exc)
    if any(isinstance(leaf, TimeoutError) for leaf in leaves):
        return MCPTimeout(
            "MCP 会话初始化超时；"
            + (
                "请检查服务地址、API key 与网络"
                if transport == "http"
                else "请检查启动命令与工作目录"
            )
        )
    if any(isinstance(leaf, FileNotFoundError) for leaf in leaves):
        return MCPUnavailable(
            "找不到 MCP 启动命令；请检查命令配置", code="command_missing"
        )

    status = _http_status(exc)
    if status in {401, 403}:
        return MCPUnavailable(
            f"MCP 服务认证失败（HTTP {status}）；请检查 API key 配置",
            code="auth_failed",
        )
    if status is not None and status >= 500:
        return MCPUnavailable(
            f"MCP 服务暂时不可用（HTTP {status}）；请稍后重试",
            code="server_error",
        )
    if transport == "http":
        if any(isinstance(leaf, ssl.SSLError) for leaf in leaves):
            return MCPUnavailable(
                "MCP HTTPS 证书校验失败；请检查受信 CA 配置", code="tls_failed"
            )
        if any(isinstance(leaf, httpx.TransportError) for leaf in leaves):
            return MCPUnavailable(
                "MCP HTTP 服务连接失败；请检查服务地址与网络",
                code="connection_failed",
            )
        # 部分客户端把 401 包装成 McpError 文本；只输出安全类别，不回显原文。
        text = " ".join(str(leaf).lower() for leaf in leaves)
        if any(
            marker in text
            for marker in ("401", "unauthorized", "invalid or expired token")
        ):
            return MCPUnavailable(
                "MCP 服务认证失败；请检查 API key 配置", code="auth_failed"
            )
        if any(
            marker in text
            for marker in (
                "certificate verify failed",
                "unable to get local issuer certificate",
                "unknown ca",
                "tls handshake",
            )
        ):
            return MCPUnavailable(
                "MCP HTTPS 证书校验失败；请检查受信 CA 配置", code="tls_failed"
            )
    return MCPUnavailable(f"MCP 会话启动失败（{type(exc).__name__}）")


__all__ = [
    "DEFAULT_CALL_TIMEOUT_SECONDS",
    "DEFAULT_CLOSE_TIMEOUT_SECONDS",
    "DEFAULT_INIT_TIMEOUT_SECONDS",
    "MCPCancelled",
    "MCPClient",
    "MCPClientError",
    "MCPProtocolError",
    "MCPServerParameters",
    "MCPTimeout",
    "MCPToolInfo",
    "MCPToolResult",
    "MCPTransport",
    "MCPUnavailable",
]
