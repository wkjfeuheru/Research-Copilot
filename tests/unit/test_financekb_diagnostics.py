import json

import pytest

from research_copilot.sources.financekb_contracts import DomainContract, ToolContract
from research_copilot.sources.financekb_diagnostics import (
    diagnose,
    diagnose_domain,
    write_proposal,
)
from research_copilot.sources.mcp_client import MCPToolInfo, MCPUnavailable

SCHEMA = {"type": "object", "properties": {}}
HASH = "b" * 64


def tool(name, *, read_only=True, output=True, fingerprint=HASH):
    return MCPToolInfo(
        name,
        f"{name} description",
        SCHEMA,
        SCHEMA if output else None,
        read_only,
        fingerprint,
    )


class Client:
    def __init__(self, tools=(), error=None):
        self.tools = tools
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(self, *exc_info):
        return None

    async def list_tools(self):
        return self.tools


def contract(domain="market", *names, pinned=True):
    return DomainContract(
        domain,
        tuple(
            ToolContract(name, HASH if pinned else None, True) for name in names
        ),
    )


async def test_matching_deployment_is_verified_and_extra_tools_are_reported():
    report = await diagnose_domain(
        contract("market", "resolve_symbol"),
        base_url="https://localhost",
        token="private-token",
        client_factory=lambda *_: Client(
            [tool("resolve_symbol"), tool("unapproved_extra")]
        ),
    )
    assert report["status"] == "ok" and report["verified"] is True
    assert report["unexpected"] == ["unapproved_extra"]
    assert report["discovered_tools"] == ["resolve_symbol", "unapproved_extra"]


@pytest.mark.parametrize(
    ("tools", "status", "field"),
    [
        ([tool("resolve_symbol", fingerprint="c" * 64)], "schema_drift", "schema_drift"),
        ([], "missing_tools", "missing"),
        ([tool("resolve_symbol", read_only=False)], "unsafe_tools", "read_only_violations"),
        ([tool("resolve_symbol", output=False)], "missing_output_schema", "missing_output_schema"),
    ],
)
async def test_contract_mismatches_are_classified(tools, status, field):
    report = await diagnose_domain(
        contract("market", "resolve_symbol"),
        base_url="https://localhost",
        token="private-token",
        client_factory=lambda *_: Client(tools),
    )
    assert report["status"] == status and report["verified"] is False
    assert report[field] == ["resolve_symbol"]


async def test_unpinned_domain_is_never_reported_as_verified():
    report = await diagnose_domain(
        contract("knowledge", "search_research", pinned=False),
        base_url="https://localhost",
        token="private-token",
        client_factory=lambda *_: Client([tool("search_research")]),
    )
    assert report["status"] == "unverified"
    assert report["verified"] is False
    assert report["unverified"] == ["search_research"]


async def test_authentication_failure_is_safe_and_does_not_hide_other_domains():
    def factory(params, name, timeout):
        if name == "financekb_market":
            return Client(error=MCPUnavailable("认证失败", code="auth_failed"))
        return Client([tool("search_news")])

    report = await diagnose(
        "https://localhost",
        "private-token",
        contracts={
            "market": contract("market", "resolve_symbol"),
            "news": contract("news", "search_news"),
        },
        client_factory=factory,
    )
    failed, ok = report["domains"]
    assert failed["status"] == "auth_failed"
    assert failed["error"] == {"code": "auth_failed", "message": "认证失败"}
    assert ok["status"] == "ok"
    assert report["summary"] == {"verified": 1, "unavailable": 1}


async def test_proposal_requires_new_file_and_never_contains_credentials(tmp_path):
    report = await diagnose_domain(
        contract("market", "resolve_symbol"),
        base_url="https://localhost",
        token="private-token",
        client_factory=lambda *_: Client([tool("resolve_symbol")]),
    )
    bundle = {"checked_at": "2026-11-11T00:00:00+00:00", "base_url": "https://localhost",
              "domains": [report]}
    target = tmp_path / "proposal.json"
    write_proposal(bundle, target)
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["domains"]["market"]["tools"]["resolve_symbol"]["schema_fingerprint"] == HASH
    assert "private-token" not in target.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        write_proposal(bundle, target)


async def test_diagnostics_locate_and_classify_annotation_drift():
    from copy import deepcopy
    from research_copilot.sources.financekb_contracts import CONTRACTS
    from research_copilot.sources.fingerprint import schema_fingerprint

    reviewed = CONTRACTS["market"].tools[0]
    changed = deepcopy(reviewed.output_schema)
    changed["properties"]["domain"]["description"] = "Updated catalog caption"
    discovered = MCPToolInfo(
        reviewed.name, reviewed.description, reviewed.input_schema, changed, True,
        schema_fingerprint({"inputSchema": reviewed.input_schema, "outputSchema": changed}),
    )
    result = await diagnose_domain(
        DomainContract("market", (reviewed,)), base_url="https://localhost", token="private",
        client_factory=lambda *_: Client([discovered]),
    )
    assert result["verified"] is True
    assert result["annotation_drift"] == [reviewed.name]
    delta = result["schema_differences"][reviewed.name]
    assert delta["classification"] == "domain_caption"
    assert delta["paths"] == ["/outputSchema/properties/domain/description"]
