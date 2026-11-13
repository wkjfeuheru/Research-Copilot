"""Actual MCP client and Streamable HTTP codec against an in-process HTTP fixture."""

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from research_copilot.sources.mcp_client import (
    MCPClient,
    MCPToolInfo,
    MCPToolResult,
    MCPServerParameters,
)
from research_copilot.subagents.searcher import discover_mcp_sources, open_mcp_sources

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {"const": "market"},
        "available": {"type": "boolean"},
        "items": {"type": "array"},
        "token": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["domain", "available", "items"],
    "additionalProperties": True,
}


def parameters(token):
    return MCPServerParameters(
        transport="http",
        url="https://financekb.example.test/mcp/market",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_financekb_initialize_paginated_discovery_and_header_auth(monkeypatch):
    traces = []

    @asynccontextmanager
    async def span(*args, **kwargs):
        record = {}
        traces.append(record)
        yield record

    monkeypatch.setattr("research_copilot.sources.mcp_client.span", span)
    requests = []

    async def respond(request):
        assert request.headers["Authorization"] == "Bearer fixture-token"
        assert "fixture-token" not in str(request.url)
        body = json.loads(request.content)
        requests.append(body)
        if "id" not in body:
            return httpx.Response(202)
        method = body["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "financekb-fixture", "version": "1"},
            }
        elif method == "tools/list":
            cursor = body.get("params", {}).get("cursor")
            result = {
                "tools": [
                    {
                        "name": "get_ohlcv" if not cursor else "resolve_symbol",
                        "description": "Read fixture market data",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"symbol": {"type": "string"}},
                            "required": ["symbol"],
                        },
                        "outputSchema": OUTPUT_SCHEMA,
                        "annotations": {"readOnlyHint": True},
                    }
                ]
            }
            if not cursor:
                result["nextCursor"] = "page-two"
        else:
            result = {
                "content": [{"type": "text", "text": "fixture response"}],
                "structuredContent": {
                    "domain": "market",
                    "available": True,
                    "items": [{"value": "120"}],
                    "token": "fixture-token",
                },
                "isError": False,
            }
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "research_copilot.sources.mcp_client.httpx.AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    async with MCPClient(
        parameters("fixture-token"), name="financekb-fixture", max_crash_restarts=0
    ) as client:
        infos = await client.list_tools()
        assert [info.name for info in infos] == ["get_ohlcv", "resolve_symbol"]
        await discover_mcp_sources(
            client,
            server="financekb_market",
            approved={"resolve_symbol": infos[1].schema_fingerprint},
        )
    async with open_mcp_sources(
        parameters("fixture-token"),
        server="financekb_market",
        approved={"resolve_symbol": infos[1].schema_fingerprint},
    ) as sources:
        raw = await sources[0].fetch({"symbol": "AAA"})
        assert raw["structured"]["items"] == [{"value": "120"}]
        assert "fixture-token" not in json.dumps(raw)
    assert "fixture-token" not in json.dumps(traces)
    assert requests[0]["method"] == "initialize"
    assert any(req.get("params", {}).get("cursor") == "page-two" for req in requests)
    assert requests[-1]["params"]["name"] == "resolve_symbol"


@pytest.mark.asyncio
async def test_financekb_response_redacts_echoed_header_credentials():
    class Client:
        async def list_tools(self):
            return [
                MCPToolInfo(
                    "resolve_symbol",
                    "Resolve",
                    {"type": "object"},
                    OUTPUT_SCHEMA,
                    True,
                    "approved",
                )
            ]

        async def call_tool(self, name, args):
            return MCPToolResult(
                False,
                {
                    "domain": "market",
                    "available": True,
                    "items": [{"value": "120"}],
                    "token": "credential",
                    "note": "echo credential",
                },
                ("credential",),
                (),
            )

    sources = await discover_mcp_sources(
        Client(),
        server="financekb_market",
        approved={"resolve_symbol": "approved"},
        redact_values=("credential",),
    )
    value = await sources[0].fetch({})
    assert "credential" not in json.dumps(value)
    assert value["structured"]["items"] == [{"value": "120"}]
