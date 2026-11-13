"""Consumer regressions from the captured 2026-11-12 FinanceKB deployment."""

import copy
import json
from pathlib import Path

import pytest

from research_copilot.sources.financekb_contracts import CONTRACTS
from research_copilot.sources.fingerprint import schema_fingerprint
from research_copilot.sources.mcp_client import MCPToolInfo, MCPToolResult
from research_copilot.subagents.searcher import discover_mcp_sources

CAPTURE = json.loads(
    (
        Path(__file__).parents[1] / "fixtures/financekb-deployment-20261112.json"
    ).read_text("utf-8")
)["domains"]


class CapturedClient:
    def __init__(self, domain, *, tools=None, result=None):
        self.rows = copy.deepcopy(CAPTURE[domain] if tools is None else tools)
        self.result = result

    async def list_tools(self):
        return [MCPToolInfo(**row) for row in self.rows]

    async def call_tool(self, name, args):
        return MCPToolResult(False, self.result, (), ())


@pytest.mark.parametrize("domain", tuple(CAPTURE))
async def test_reviewed_deployment_exposes_all_captured_read_only_tools(domain):
    contract = CONTRACTS.get(domain)
    assert contract is not None, f"Missing reviewed domain: {domain}"
    tools = await discover_mcp_sources(
        CapturedClient(domain), server=f"financekb_{domain}", approved=contract.approved
    )
    assert {t.original_name for t in tools} == {r["name"] for r in CAPTURE[domain]}


def changed_tool(path, value):
    row = copy.deepcopy(CAPTURE["market"][0])
    target = row
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    row["schema_fingerprint"] = schema_fingerprint(
        {"inputSchema": row["input_schema"], "outputSchema": row["output_schema"]}
    )
    return row


async def test_common_domain_caption_drift_keeps_reviewed_tool_metadata():
    row = changed_tool(
        ["output_schema", "properties", "domain", "description"],
        "Domain catalog caption changed; never forward this unreviewed text",
    )
    row["description"] = "UNREVIEWED TOOL TEXT"
    tools = await discover_mcp_sources(
        CapturedClient("market", tools=[row]),
        server="financekb_market",
        approved={row["name"]: CONTRACTS["market"].approved[row["name"]]},
    )
    assert len(tools) == 1
    assert tools[0].description != "UNREVIEWED TOOL TEXT"
    assert tools[0].schema_hash == CONTRACTS["market"].approved[row["name"]]
    assert tools[0].observed_schema_hash == row["schema_fingerprint"]


@pytest.mark.parametrize(
    "path,value",
    [
        (["output_schema", "properties", "domain", "type"], "integer"),
        (
            ["output_schema", "properties", "as_of", "description"],
            "timestamp in another timezone",
        ),
    ],
)
async def test_structure_and_business_description_drift_still_fail_closed(path, value):
    row = changed_tool(path, value)
    with pytest.raises(ValueError, match="schema changed"):
        await discover_mcp_sources(
            CapturedClient("market", tools=[row]),
            server="financekb_market",
            approved={row["name"]: CONTRACTS["market"].approved[row["name"]]},
        )


async def test_commodity_source_preserves_units_time_and_contract_type():
    result = {
        "available": True,
        "domain": "futures",
        "reason": "",
        "as_of": "2026-11-12T12:00:00Z",
        "items": [
            {
                "symbol": "225.lcm",
                "contract_code": "lcm",
                "contract_name": "碳酸锂主连",
                "commodity_code": "lc",
                "commodity_name": "碳酸锂",
                "exchange": "GFEX",
                "contract_type": "main_continuous",
                "price": "100000",
                "currency": None,
                "price_unit": None,
                "source_time": "2026-11-12T15:00:00+08:00",
                "source": "eastmoney",
            }
        ],
    }
    contract = CONTRACTS.get("futures")
    assert contract is not None
    tools = await discover_mcp_sources(
        CapturedClient("futures", result=result),
        server="financekb_futures",
        approved=contract.approved,
    )
    raw = await tools[0].fetch({"symbol": "225.lcm"})
    quote = raw["structured"]["items"][0]
    assert quote["currency"] is None and quote["price_unit"] is None
    assert quote["contract_type"] == "main_continuous"
    assert quote["source_time"] == "2026-11-12T15:00:00+08:00"
    assert tools[0].normalize(raw, {}).drafts
