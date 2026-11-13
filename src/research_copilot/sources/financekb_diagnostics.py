"""Read-only FinanceKB deployment diagnostics.

Only ``initialize`` and paginated ``tools/list`` run here: no business tool
calls, no model calls and no database writes. The report compares what the
deployment exposes with the pinned snapshot; it never adopts a discovered
schema automatically.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from research_copilot.sources.financekb_contracts import (
    CONTRACTS,
    CONTRACT_SHA256,
    DomainContract,
)
from research_copilot.sources.mcp_client import (
    MCPClient,
    MCPClientError,
    MCPServerParameters,
)
from research_copilot.sources.schema_compatibility import compare_schemas

ClientFactory = Callable[[MCPServerParameters, str, float], Any]


def _parameters(
    base_url: str, token: str, domain: str, ca_bundle: str | None
) -> MCPServerParameters:
    return MCPServerParameters(
        transport="http",
        url=f"{base_url}/mcp/{domain}",
        headers={"Authorization": f"Bearer {token}"},
        ca_bundle=ca_bundle,
    )


def _compare(contract: DomainContract, tools) -> dict[str, Any]:
    discovered = {
        tool.name: tool for tool in tools if tool.name
    }
    approved = contract.approved
    changed = sorted(
        name
        for name, fingerprint in approved.items()
        if name in discovered
        and discovered[name].schema_fingerprint != fingerprint
    )
    reviewed = {tool.name: tool for tool in contract.tools}
    differences = {
        name: compare_schemas(
            reviewed[name].input_schema, reviewed[name].output_schema,
            discovered[name].input_schema, discovered[name].output_schema,
        )
        for name in changed if reviewed[name].complete
    }
    annotation_drift = [
        name for name in changed
        if differences.get(name, {}).get("classification") == "domain_caption"
    ]
    schema_drift = [name for name in changed if name not in annotation_drift]
    missing = sorted(name for name in approved if name not in discovered)
    unexpected = sorted(name for name in discovered if name not in approved)
    read_only_violations = sorted(
        name
        for name in approved
        if name in discovered and discovered[name].read_only_hint is not True
    )
    missing_output_schema = sorted(
        name
        for name in approved
        if name in discovered and not discovered[name].output_schema
    )
    if not contract.verified:
        status = "unverified"
    elif schema_drift:
        status = "schema_drift"
    elif missing:
        status = "missing_tools"
    elif read_only_violations:
        status = "unsafe_tools"
    elif missing_output_schema:
        status = "missing_output_schema"
    else:
        status = "ok"
    return {
        "status": status,
        "verified": status == "ok",
        "tool_count": len(discovered),
        "approved_count": len(approved),
        "missing": missing,
        "unexpected": unexpected,
        "schema_drift": schema_drift,
        "annotation_drift": annotation_drift,
        "schema_differences": differences,
        "unverified": list(contract.unverified_tools),
        "read_only_violations": read_only_violations,
        "missing_output_schema": missing_output_schema,
        "discovered_tools": sorted(discovered),
        "discovered": [
            {
                "name": tool.name,
                "description": tool.description,
                "read_only_hint": tool.read_only_hint,
                "schema_fingerprint": tool.schema_fingerprint,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
            }
            for tool in sorted(discovered.values(), key=lambda item: item.name)
        ],
        "error": None,
    }


async def diagnose_domain(
    contract: DomainContract,
    *,
    base_url: str,
    token: str,
    ca_bundle: str | None = None,
    call_timeout: float = 150.0,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    parameters = _parameters(base_url, token, contract.domain, ca_bundle)
    if client_factory is None:

        def client_factory(params, name, timeout):
            return MCPClient(
                params, name=name, max_crash_restarts=0, call_timeout=timeout
            )

    report = {
        "domain": contract.domain,
        "status": "unavailable",
        "verified": False,
        "tool_count": 0,
        "approved_count": len(contract.approved),
        "missing": [],
        "unexpected": [],
        "schema_drift": [],
        "annotation_drift": [],
        "schema_differences": {},
        "unverified": list(contract.unverified_tools),
        "read_only_violations": [],
        "missing_output_schema": [],
        "discovered_tools": [],
        "discovered": [],
        "error": None,
    }
    try:
        async with client_factory(
            parameters, f"financekb_{contract.domain}", call_timeout
        ) as client:
            tools = await client.list_tools()
    except MCPClientError as exc:
        report.update(status=exc.code, error={"code": exc.code, "message": str(exc)})
        return report
    report.update(_compare(contract, tools))
    return report


async def diagnose(
    base_url: str,
    token: str,
    *,
    ca_bundle: str | None = None,
    call_timeout: float = 150.0,
    contracts: dict[str, DomainContract] | None = None,
    client_factory: ClientFactory | None = None,
) -> dict[str, Any]:
    """Check every approved domain; one failure does not hide the others."""

    catalog = CONTRACTS if contracts is None else contracts
    domains = []
    for contract in catalog.values():
        domains.append(
            await diagnose_domain(
                contract,
                base_url=base_url,
                token=token,
                ca_bundle=ca_bundle,
                call_timeout=call_timeout,
                client_factory=client_factory,
            )
        )
    verified = sum(1 for item in domains if item["verified"])
    return {
        "base_url": base_url,
        "contract_sha256": CONTRACT_SHA256,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "domains": domains,
        "summary": {
            "verified": verified,
            "unavailable": len(domains) - verified,
        },
    }


def write_proposal(report: dict[str, Any], path: Path) -> None:
    """Write review material for pinning; never edits the approved snapshot."""

    target = Path(path)
    if target.exists():
        raise ValueError("Proposal output already exists")
    domains: dict[str, Any] = {}
    for item in report["domains"]:
        if item["discovered"]:
            domains[item["domain"]] = {
                "tools": {
                    tool["name"]: {
                        "schema_fingerprint": tool["schema_fingerprint"],
                        "read_only": tool["read_only_hint"],
                        "description": tool["description"],
                        "input_schema": tool["input_schema"],
                        "output_schema": tool["output_schema"],
                    }
                    for tool in item["discovered"]
                }
            }
    payload = {
        "format": 1,
        "generated_at": report["checked_at"],
        "base_url": report["base_url"],
        "instructions": (
            "Review every input/output schema and description, then copy the "
            "approved fingerprints into financekb_contracts.json. This proposal "
            "is not an approval and is never loaded at runtime."
        ),
        "domains": domains,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


__all__ = ["diagnose", "diagnose_domain", "write_proposal"]
