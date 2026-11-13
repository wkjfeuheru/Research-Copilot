"""FinanceKB reviewed catalog and the Research-Copilot connection lifecycle."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from research_copilot.sources.financekb_contracts import (
    CONTRACTS,
    DomainContract,
    contract_sha256,
)
from research_copilot.sources.mcp_client import MCPClientError, MCPServerParameters
from research_copilot.subagents.searcher import (
    NORMALIZATION_VERSION,
    SourceTool,
    SourceUnavailable,
    open_mcp_sources,
)

logger = logging.getLogger(__name__)

_REASON_LABELS = {
    "auth_failed": "认证失败",
    "tls_failed": "证书校验失败",
    "connection_failed": "连接失败",
    "server_error": "服务错误",
    "timeout": "超时",
    "protocol_error": "协议错误",
    "tool_error": "工具错误",
    "command_missing": "配置缺失",
    "unverified": "schema 尚未批准",
    "contract_mismatch": "部署合同不匹配",
}


@dataclass(frozen=True, slots=True)
class SourceStatus:
    domain: str
    available: bool
    code: str | None
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinanceKBSources:
    """Stable catalog plus the health observed when this session opened."""

    tools: tuple[SourceTool, ...]
    statuses: tuple[SourceStatus, ...]
    catalog: dict[str, dict[str, Any]]
    contract: dict[str, Any]

    @property
    def note(self) -> str:
        parts = []
        for status in self.statuses:
            if status.available:
                parts.append(
                    f"{status.domain} 可用（{', '.join(status.tools) or '无工具'}）"
                )
            else:
                parts.append(
                    f"{status.domain} 不可用"
                    f"（{_REASON_LABELS.get(status.code or '', status.code or '未知')}）"
                )
        return (
            "FinanceKB 数据域状态：" + "；".join(parts) + "。"
            "只调用实际提供的 financekb_* 工具；不可用或未验证的数据域必须作为"
            "任务缺口如实报告，不得改用其他来源，也不得猜测工具名或 schema。"
        )


def _catalog(contracts: dict[str, DomainContract]) -> dict[str, dict[str, Any]]:
    return {
        f"financekb_{tool.name}": {
            "domain": domain,
            "server": f"financekb_{domain}",
            "original_name": tool.name,
            "schema_hash": tool.schema_fingerprint,
        }
        for domain, contract in contracts.items()
        for tool in contract.tools
    }


def _stable_tools(contract: DomainContract, code: str) -> list[SourceTool]:
    """Keep reviewed tool definitions mounted when a domain is temporarily down."""

    tools = []
    for tool in contract.tools:
        if not tool.complete:
            continue
        assert tool.input_schema is not None

        async def unavailable(args, name=tool.name, domain=contract.domain):
            raise SourceUnavailable(
                f"FinanceKB {domain} is unavailable for {name}", code=code
            )

        tools.append(
            SourceTool(
                name=f"financekb_{tool.name}",
                server=f"financekb_{contract.domain}",
                original_name=tool.name,
                description=tool.description,
                schema=tool.input_schema,
                fetch=unavailable,
                schema_hash=tool.schema_fingerprint,
            )
        )
    return tools


@asynccontextmanager
async def open_financekb_sources(
    base_url: str,
    token: str,
    *,
    ca_bundle: str | None = None,
    call_timeout: float | None = None,
    contracts: dict[str, DomainContract] | None = None,
) -> AsyncIterator[FinanceKBSources]:
    catalog_contracts = CONTRACTS if contracts is None else contracts
    async with AsyncExitStack() as stack:
        tools: list[SourceTool] = []
        statuses: list[SourceStatus] = []
        for domain, contract in catalog_contracts.items():
            server = f"financekb_{domain}"
            if not contract.verified:
                logger.warning(
                    "FinanceKB %s contract is not pinned; domain stays unavailable",
                    domain,
                )
                statuses.append(SourceStatus(domain, False, "unverified", ()))
                continue
            parameters = MCPServerParameters(
                transport="http",
                url=f"{base_url}/mcp/{domain}",
                headers={"Authorization": f"Bearer {token}"},
                ca_bundle=ca_bundle,
            )
            try:
                discovered = await stack.enter_async_context(
                    open_mcp_sources(
                        parameters,
                        server=server,
                        approved=contract.approved,
                        call_timeout=call_timeout,
                    )
                )
            except MCPClientError as exc:
                code = exc.code
                logger.warning("FinanceKB %s unavailable (%s)", domain, code)
                stable = _stable_tools(contract, code)
                tools.extend(stable)
                statuses.append(
                    SourceStatus(
                        domain, False, code, tuple(tool.name for tool in stable)
                    )
                )
                continue
            except ValueError:
                logger.warning("FinanceKB %s contract mismatch", domain)
                stable = _stable_tools(contract, "contract_mismatch")
                tools.extend(stable)
                statuses.append(
                    SourceStatus(
                        domain,
                        False,
                        "contract_mismatch",
                        tuple(tool.name for tool in stable),
                    )
                )
                continue
            tools.extend(discovered)
            statuses.append(
                SourceStatus(
                    domain, True, None, tuple(tool.name for tool in discovered)
                )
            )
        yield FinanceKBSources(
            tuple(tools),
            tuple(statuses),
            _catalog(catalog_contracts),
            {
                "sha256": contract_sha256(catalog_contracts),
                "normalization_version": NORMALIZATION_VERSION,
            },
        )
