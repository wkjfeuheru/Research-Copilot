import json
from pathlib import Path

import pytest

from research_copilot.sources.fingerprint import schema_fingerprint
from research_copilot.sources.financekb_contracts import (
    APPROVED_DOMAINS,
    APPROVED_SCHEMAS,
    CONTRACTS,
    CONTRACT_SHA256,
    DomainContract,
    SNAPSHOT_PATH,
    ToolContract,
    contract_sha256,
    load_contracts,
)

PINNED = "a" * 64


def snapshot(**overrides):
    domains = {
        "market": {
            "tools": {
                "resolve_symbol": {"schema_fingerprint": PINNED, "read_only": True}
            }
        },
        "fundamentals": {
            "tools": {
                "get_financial_statements": {
                    "schema_fingerprint": PINNED,
                    "read_only": True,
                }
            }
        },
        "macro": {
            "tools": {
                "get_macro_indicator": {"schema_fingerprint": PINNED, "read_only": True}
            }
        },
        "news": {
            "tools": {
                "search_news": {"schema_fingerprint": PINNED, "read_only": True}
            }
        },
        "knowledge": {
            "tools": {
                "search_research": {"schema_fingerprint": None, "read_only": True}
            }
        },
        "futures": {
            "tools": {
                "get_commodity_price": {"schema_fingerprint": PINNED, "read_only": True}
            }
        },
    }
    domains.update(overrides)
    return {"format": 1, "domains": domains}


def write(tmp_path, payload):
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_packaged_snapshot_covers_reviewed_domains_without_external_sources():
    assert SNAPSHOT_PATH.is_file()
    assert tuple(CONTRACTS) == APPROVED_DOMAINS
    assert set(APPROVED_SCHEMAS) == set(APPROVED_DOMAINS)
    assert len(CONTRACT_SHA256) == 64
    knowledge = CONTRACTS["knowledge"]
    assert knowledge.verified is True
    assert knowledge.unverified_tools == ()
    assert set(knowledge.approved) == {"search_research", "search_policy"}
    assert all(
        tool.complete
        for contract in CONTRACTS.values()
        for tool in contract.tools
    )


def test_contract_identity_tracks_reviewed_schema_not_availability():
    tool = ToolContract("resolve_symbol", PINNED, True)
    first = {"market": DomainContract("market", (tool,))}
    assert contract_sha256(first) == contract_sha256(
        {"market": DomainContract("market", (tool,))}
    )
    changed = {
        "market": DomainContract(
            "market", (ToolContract("resolve_symbol", "b" * 64, True),)
        )
    }
    assert contract_sha256(first) != contract_sha256(changed)


def test_unverified_domain_is_absent_from_derived_approvals(tmp_path):
    loaded = load_contracts(write(tmp_path, snapshot()))
    assert loaded["knowledge"].verified is False
    assert loaded["market"].approved == {"resolve_symbol": PINNED}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["domains"].pop("knowledge"),
        lambda data: data["domains"].update(extra={"tools": {}}),
        lambda data: data.update(format=2),
        lambda data: data["domains"]["market"].update(extra=True),
        lambda data: data["domains"]["market"]["tools"].update(
            delete_everything={"schema_fingerprint": PINNED, "read_only": True}
        ),
        lambda data: data["domains"]["market"]["tools"]["resolve_symbol"].update(
            read_only=False
        ),
        lambda data: data["domains"]["market"]["tools"]["resolve_symbol"].update(
            schema_fingerprint="not-a-hash"
        ),
        lambda data: data["domains"]["market"]["tools"]["resolve_symbol"].update(
            unreviewed="field"
        ),
        lambda data: data["domains"]["market"].update(tools={}),
    ],
)
def test_invalid_snapshots_fail_loudly(tmp_path, mutate):
    data = snapshot()
    mutate(data)
    with pytest.raises(ValueError):
        load_contracts(write(tmp_path, data))


def test_missing_snapshot_file_is_an_error(tmp_path):
    with pytest.raises(OSError):
        load_contracts(Path(tmp_path) / "absent.json")


def test_reviewed_schemas_must_match_the_pinned_fingerprint(tmp_path):
    schema = {"type": "object", "properties": {}}
    fingerprint = schema_fingerprint(
        {"inputSchema": schema, "outputSchema": schema}
    )
    data = snapshot()
    entry = data["domains"]["market"]["tools"]["resolve_symbol"]
    entry.update(
        schema_fingerprint=fingerprint,
        description="Resolve a security",
        input_schema=schema,
        output_schema=schema,
    )
    loaded = load_contracts(write(tmp_path, data))
    assert loaded["market"].tools[0].complete is True

    entry["input_schema"] = {"type": "string"}
    with pytest.raises(ValueError, match="differs from pin"):
        load_contracts(write(tmp_path, data))
