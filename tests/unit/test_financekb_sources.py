from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from research_copilot.runtime.host_settings import HostSettings
from research_copilot.sources import financekb
from research_copilot.sources.fingerprint import schema_fingerprint
from research_copilot.sources.financekb_contracts import DomainContract, ToolContract
from research_copilot.sources.mcp_client import (
    MCPToolInfo,
    MCPToolResult,
    MCPUnavailable,
)
from research_copilot.subagents.searcher import (
    SourceTool,
    SourceUnavailable,
    build_search_tools,
    discover_mcp_sources,
)
from research_copilot.subagents.source_policy import assess_source

SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def complete_contract():
    fingerprint = schema_fingerprint(
        {"inputSchema": SCHEMA, "outputSchema": SCHEMA}
    )
    return DomainContract(
        "macro",
        (
            ToolContract(
                "get_macro_indicator",
                fingerprint,
                True,
                "CPI",
                SCHEMA,
                SCHEMA,
            ),
        ),
    )


@asynccontextmanager
async def fake_sources(parameters, *, server, approved, call_timeout=None):
    yield [
        SourceTool(
            "financekb_" + next(iter(approved)),
            server,
            next(iter(approved)),
            "CPI",
            SCHEMA,
            lambda args: None,
            schema_hash=next(iter(approved.values())),
        )
    ]


async def test_offline_domain_keeps_reviewed_tool_definition(monkeypatch):
    @asynccontextmanager
    async def unavailable(*args, **kwargs):
        raise MCPUnavailable("认证失败", code="auth_failed")
        yield

    monkeypatch.setattr(financekb, "open_mcp_sources", unavailable)
    async with financekb.open_financekb_sources(
        "https://localhost", "private-token", contracts={"macro": complete_contract()}
    ) as bundle:
        (status,) = bundle.statuses
        assert status.available is False and status.code == "auth_failed"
        (tool,) = bundle.tools
        assert tool.name == "financekb_get_macro_indicator"
        assert tool.schema == SCHEMA
        with pytest.raises(SourceUnavailable) as raised:
            await tool.fetch({})
        assert raised.value.code == "auth_failed"
        assert "private-token" not in bundle.note
        assert "macro 不可用" in bundle.note


async def test_one_failed_domain_does_not_hide_a_healthy_domain(monkeypatch):
    @asynccontextmanager
    async def open_sources(parameters, *, server, approved, call_timeout=None):
        if server == "financekb_macro":
            raise MCPUnavailable("连接失败", code="connection_failed")
        async with fake_sources(
            parameters, server=server, approved=approved, call_timeout=call_timeout
        ) as tools:
            yield tools

    monkeypatch.setattr(financekb, "open_mcp_sources", open_sources)
    news = DomainContract(
        "news",
        (ToolContract("search_news", "c" * 64, True, "News", SCHEMA, SCHEMA),),
    )
    contracts = {"macro": complete_contract(), "news": news}
    async with financekb.open_financekb_sources(
        "https://localhost", "private-token", contracts=contracts
    ) as bundle:
        statuses = {status.domain: status for status in bundle.statuses}
        assert statuses["macro"].available is False
        assert statuses["news"].available is True
        assert {tool.name for tool in bundle.tools} == {
            "financekb_get_macro_indicator",
            "financekb_search_news",
        }
        assert set(bundle.catalog) == {
            "financekb_get_macro_indicator",
            "financekb_search_news",
        }
        assert len(bundle.contract["sha256"]) == 64
        assert bundle.contract["normalization_version"] >= 1


async def test_unpinned_domain_is_not_mounted(monkeypatch):
    @asynccontextmanager
    async def unexpected(*args, **kwargs):
        raise AssertionError("unpinned domains must not connect")
        yield

    monkeypatch.setattr(financekb, "open_mcp_sources", unexpected)
    unverified = DomainContract(
        "knowledge", (ToolContract("search_research", None, True),)
    )
    async with financekb.open_financekb_sources(
        "https://localhost", "private-token", contracts={"knowledge": unverified}
    ) as bundle:
        assert bundle.tools == ()
        (status,) = bundle.statuses
        assert status.code == "unverified"
        assert "knowledge 不可用" in bundle.note


def test_financekb_requires_explicit_https_and_header_credentials():
    endpoint = "https://financekb.example.test/"
    settings = HostSettings(
        _env_file=None, financekb_base_url=endpoint, financekb_api_token="private-token"
    )
    assert settings.model_dump().get("financekb_base_url") == endpoint.rstrip("/")
    assert "private-token" not in repr(settings)
    for kwargs in (
        {"financekb_base_url": endpoint},
        {"financekb_api_token": "private-token"},
        {
            "financekb_base_url": "http://financekb.example.test",
            "financekb_api_token": "token",
        },
        {"financekb_base_url": endpoint + "?token=abc", "financekb_api_token": "token"},
    ):
        with pytest.raises(ValidationError):
            HostSettings(_env_file=None, **kwargs)


@pytest.mark.asyncio
async def test_financekb_discovery_preserves_schema_and_rejects_unavailable_data():
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    output_schema = {
        "type": "object",
        "properties": {
            "domain": {"type": "string"},
            "available": {"type": "boolean"},
            "items": {"type": "array"},
        },
        "required": ["domain", "available", "items"],
        "additionalProperties": True,
    }
    result = {"domain": "macro", "available": True, "items": []}
    hint = True

    class Client:
        async def list_tools(self):
            return [
                MCPToolInfo(
                    "get_macro_indicator", "CPI", schema, output_schema, hint, "pinned"
                )
            ]

        async def call_tool(self, name, args):
            return MCPToolResult(False, result, (), ())

    approved = {"get_macro_indicator": "pinned"}
    sources = await discover_mcp_sources(
        Client(), server="financekb_macro", approved=approved
    )
    (tool,) = build_search_tools(SimpleNamespace(), sources)
    assert tool.name == "financekb_get_macro_indicator" and tool.args_schema == schema
    raw = await sources[0].fetch({})
    assert raw["structured"] == result
    empty = sources[0].normalize(raw, {})
    assert empty.drafts == () and "无匹配" in empty.warnings[0]
    result["items"] = [{"value": 1}]
    raw = await sources[0].fetch({})
    (draft,) = sources[0].normalize(raw, {}).drafts
    assert draft.kind == "financial_data" and draft.raw["structured"] == result
    result["available"] = False
    raw = await sources[0].fetch({})
    unavailable = sources[0].normalize(raw, {})
    assert unavailable.drafts == () and "不可用" in unavailable.warnings[0]
    result.update(available=True, items=[])
    result.pop("items")
    with pytest.raises(SourceUnavailable) as malformed:
        await sources[0].fetch({})
    assert malformed.value.code == "contract_mismatch"
    result.update(available=True, domain="news", items=[])
    with pytest.raises(SourceUnavailable) as drifted:
        await sources[0].fetch({})
    assert drifted.value.code == "contract_mismatch"
    with pytest.raises(ValueError, match="schema changed"):
        await discover_mcp_sources(
            Client(), server="financekb_macro", approved={"get_macro_indicator": "old"}
        )
    hint = None
    with pytest.raises(ValueError, match="read-only"):
        await discover_mcp_sources(
            Client(), server="financekb_macro", approved=approved
        )


@pytest.mark.asyncio
async def test_financekb_known_publication_dates_reach_evidence_cutoff():
    schema = {"type": "object"}
    data = {
        "domain": "news",
        "available": True,
        "items": [
            {"published_at": "2026-11-09T20:00:00+08:00"},
            {"published_at": "2026-11-09T09:00:00+08:00"},
        ],
    }

    class Client:
        async def list_tools(self):
            return [MCPToolInfo("search_news", "News", schema, schema, True, "pinned")]

        async def call_tool(self, name, args):
            return MCPToolResult(False, data, (), ())

    (source,) = await discover_mcp_sources(
        Client(), server="financekb_news", approved={"search_news": "pinned"}
    )
    raw = await source.fetch({})
    assert raw["structured"] == data
    assert raw["published_at"] == "2026-11-09T20:00:00+08:00"
    evidence = {"raw": raw, "kind": "financial_data"}
    assert (
        assess_source(evidence, "2026-11-09T15:00:00+08:00")["reason"]
        == "published_after_as_of"
    )


KNOWLEDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "collection": {"type": "string"},
        "as_of": {"type": "string"},
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
        "returned": {"type": "integer"},
        "has_more": {"type": "boolean"},
        "next_page": {"type": ["integer", "null"]},
        "items": {"type": "array", "items": {"type": "object"}},
    },
    "required": [
        "collection",
        "as_of",
        "page",
        "page_size",
        "returned",
        "has_more",
        "next_page",
        "items",
    ],
    "additionalProperties": True,
}


def knowledge_page(items, **changes):
    page = {
        "collection": "finance_report",
        "as_of": "2025-06-30",
        "page": 1,
        "page_size": 10,
        "returned": len(items),
        "has_more": False,
        "next_page": None,
        "items": items,
    }
    page.update(changes)
    return page


def knowledge_snippet(**changes):
    item = {
        "id": "doc-1",
        "chunk": 2,
        "source": "broker-research",
        "title": "贵州茅台渠道调研",
        "content": "贵州茅台渠道库存下降，批价保持稳定。",
        "date": "2025-05-12",
        "page": 18,
        "score": 0.91,
    }
    item.update(changes)
    return item


async def knowledge_source(page):
    class Client:
        async def list_tools(self):
            return [
                MCPToolInfo(
                    "search_research",
                    "Research",
                    {"type": "object"},
                    KNOWLEDGE_SCHEMA,
                    True,
                    "pinned",
                )
            ]

        async def call_tool(self, name, args):
            return MCPToolResult(False, page, (), ())

    (source,) = await discover_mcp_sources(
        Client(), server="financekb_knowledge", approved={"search_research": "pinned"}
    )
    return source


BRIEF = {
    "as_of": "2025-06-30",
    "entity_terms": ["贵州茅台"],
    "topic_terms": ["渠道"],
}


@pytest.mark.asyncio
async def test_knowledge_normalisation_registers_eligible_snippets_only():
    items = [
        knowledge_snippet(),
        knowledge_snippet(id="doc-2", chunk=1, score=0.42),
        knowledge_snippet(id="doc-future", date="2025-07-15"),
        knowledge_snippet(id="doc-undated", date=None),
        knowledge_snippet(id="doc-offtopic", title="无关行业", content="其他行业数据。"),
    ]
    source = await knowledge_source(knowledge_page(items))
    outcome = source.normalize(await source.fetch({}), BRIEF)
    assert [draft.pointer for draft in outcome.drafts] == ["/items/0", "/items/1"]
    assert all(draft.kind == "search_snippet" for draft in outcome.drafts)
    assert len(outcome.warnings) == 3
    first = outcome.drafts[0].raw
    assert first["structured"]["id"] == "doc-1"
    assert first["published_at"] == "2025-05-12"
    assert first["qualification"]["relevant"] is True
    assert first["source_ref"]["item_index"] == 0
    assert outcome.pagination["page"] == 1 and outcome.pagination["has_more"] is False
    record = {"raw": first, "kind": "search_snippet"}
    assert assess_source(record, BRIEF["as_of"])["eligible"] is True
    # Retrieval noise must not mint a new evidence identity.
    rescored = await knowledge_source(knowledge_page([knowledge_snippet(score=0.13)]))
    rerun = rescored.normalize(await rescored.fetch({}), BRIEF)
    assert rerun.drafts[0].identity == outcome.drafts[0].identity


@pytest.mark.asyncio
async def test_knowledge_envelope_rejects_contract_drift():
    async def outcome_for(page):
        normalised = await knowledge_source(page)
        raw = await normalised.fetch({})
        return raw

    with pytest.raises(SourceUnavailable) as wrong_collection:
        await outcome_for(knowledge_page([knowledge_snippet()], collection="finance_policy"))
    assert wrong_collection.value.code == "contract_mismatch"
    with pytest.raises(SourceUnavailable) as wrong_count:
        await outcome_for(knowledge_page([knowledge_snippet()], returned=2))
    assert wrong_count.value.code == "contract_mismatch"
    with pytest.raises(SourceUnavailable) as stalled:
        await outcome_for(
            knowledge_page(
                [knowledge_snippet()], has_more=True, next_page=1
            )
        )
    assert stalled.value.code == "contract_mismatch"
    with pytest.raises(SourceUnavailable) as oversize:
        await outcome_for(knowledge_page([knowledge_snippet()], page_size=51))
    assert oversize.value.code == "contract_mismatch"
    with pytest.raises(SourceUnavailable) as window:
        await outcome_for(knowledge_page([knowledge_snippet()], page=21))
    assert window.value.code == "contract_mismatch"


@pytest.mark.asyncio
async def test_knowledge_window_limit_is_reported_as_bounded_coverage():
    source = await knowledge_source(
        knowledge_page([knowledge_snippet()], page=20, page_size=10)
    )
    outcome = source.normalize(await source.fetch({}), BRIEF)
    assert outcome.pagination["window_limit_reached"] is True
    assert any("窗口上限" in warning for warning in outcome.warnings)


@pytest.mark.asyncio
async def test_discovery_binds_each_tool_to_its_own_output_schema():
    schema_a = {
        "type": "object",
        "properties": {"kind": {"const": "a"}},
        "required": ["kind"],
    }
    schema_b = {
        "type": "object",
        "properties": {"kind": {"const": "b"}},
        "required": ["kind"],
    }

    class Client:
        async def list_tools(self):
            return [
                MCPToolInfo("tool_a", "A", SCHEMA, schema_a, True, "pin_a"),
                MCPToolInfo("tool_b", "B", SCHEMA, schema_b, True, "pin_b"),
            ]

        async def call_tool(self, name, args):
            payload = {
                "domain": "market",
                "available": True,
                "items": [],
                "kind": name.removeprefix("tool_"),
            }
            return MCPToolResult(False, payload, (), ())

    tool_a, tool_b = await discover_mcp_sources(
        Client(),
        server="financekb_market",
        approved={"tool_a": "pin_a", "tool_b": "pin_b"},
    )
    assert (await tool_a.fetch({}))["structured"]["kind"] == "a"
    assert (await tool_b.fetch({}))["structured"]["kind"] == "b"
