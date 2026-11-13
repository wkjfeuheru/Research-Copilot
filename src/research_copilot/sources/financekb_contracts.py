"""Reviewed FinanceKB tool catalog loaded from one pinned snapshot.

The snapshot is the only approval record. Discovery may report drift, but it
never updates this file: pinning a schema requires an explicit review and code
change. Fill in ``null`` fingerprints only after reviewing the discovered
inputSchema/outputSchema pair.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from research_copilot.sources.fingerprint import canonical_json, schema_fingerprint

APPROVED_DOMAINS = ("market", "fundamentals", "macro", "news", "knowledge", "futures")
SNAPSHOT_PATH = Path(__file__).with_name("financekb_contracts.json")
_FINGERPRINT = re.compile(r"[a-f0-9]{64}")
_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_DANGEROUS = re.compile(r"\b(delete|write|update|execute|trade|order|insert)\b")


@dataclass(frozen=True, slots=True)
class ToolContract:
    name: str
    schema_fingerprint: str | None
    read_only: bool
    description: str = ""
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None

    @property
    def complete(self) -> bool:
        """Enough reviewed definition to keep the tool mounted while offline."""

        return bool(
            self.schema_fingerprint and self.input_schema and self.output_schema
        )


@dataclass(frozen=True, slots=True)
class DomainContract:
    domain: str
    tools: tuple[ToolContract, ...]

    @property
    def verified(self) -> bool:
        """Every approved tool has a reviewed schema hash."""

        return all(tool.schema_fingerprint for tool in self.tools)

    @property
    def unverified_tools(self) -> tuple[str, ...]:
        return tuple(
            tool.name for tool in self.tools if tool.schema_fingerprint is None
        )

    @property
    def approved(self) -> dict[str, str]:
        """Tool name to pinned fingerprint; empty until the whole domain is reviewed."""

        if not self.verified:
            return {}
        return {
            tool.name: tool.schema_fingerprint
            for tool in self.tools
            if tool.schema_fingerprint
        }


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a JSON object")
    return value


def load_contracts(path: Path | None = None) -> dict[str, DomainContract]:
    """Load and validate the snapshot; unknown or unpinned shapes fail loudly."""

    target = SNAPSHOT_PATH if path is None else path
    payload = _object(
        json.loads(Path(target).read_text(encoding="utf-8")), "contract snapshot"
    )
    if set(payload) != {"format", "domains"} or payload["format"] != 1:
        raise ValueError("Unsupported FinanceKB contract snapshot format")
    domains = _object(payload["domains"], "domains")
    if set(domains) != set(APPROVED_DOMAINS):
        raise ValueError("FinanceKB contract domains differ from the approved catalog")
    loaded: dict[str, DomainContract] = {}
    for domain in APPROVED_DOMAINS:
        entry = _object(domains[domain], domain)
        if set(entry) != {"tools"}:
            raise ValueError(f"{domain} contract has unexpected fields")
        raw_tools = _object(entry["tools"], f"{domain} tools")
        if not 1 <= len(raw_tools) <= 10:
            raise ValueError(f"{domain} contract must pin between 1 and 10 tools")
        tools: list[ToolContract] = []
        for name, raw in raw_tools.items():
            if not _TOOL_NAME.fullmatch(name) or _DANGEROUS.search(
                name.replace("_", " ")
            ):
                raise ValueError(f"{domain} contains an unreviewed tool name")
            item = _object(raw, f"{domain}.{name}")
            required = {"schema_fingerprint", "read_only"}
            optional = {"description", "input_schema", "output_schema"}
            if not required <= set(item) or set(item) - required - optional:
                raise ValueError(f"{domain}.{name} contract has unexpected fields")
            fingerprint = item["schema_fingerprint"]
            if fingerprint is not None and not (
                isinstance(fingerprint, str) and _FINGERPRINT.fullmatch(fingerprint)
            ):
                raise ValueError(f"{domain}.{name} fingerprint is not canonical")
            if item["read_only"] is not True:
                raise ValueError(f"{domain}.{name} must be a reviewed read-only tool")
            input_schema = item.get("input_schema")
            output_schema = item.get("output_schema")
            if input_schema is not None or output_schema is not None:
                if not (
                    isinstance(input_schema, dict)
                    and isinstance(output_schema, dict)
                    and fingerprint
                ):
                    raise ValueError(
                        f"{domain}.{name} must pin both schemas and their fingerprint"
                    )
                if schema_fingerprint(
                    {"inputSchema": input_schema, "outputSchema": output_schema}
                ) != fingerprint:
                    raise ValueError(f"{domain}.{name} schema content differs from pin")
            description = item.get("description", "")
            if not isinstance(description, str):
                raise ValueError(f"{domain}.{name} description is not text")
            tools.append(
                ToolContract(
                    name,
                    fingerprint,
                    True,
                    description,
                    input_schema,
                    output_schema,
                )
            )
        loaded[domain] = DomainContract(domain, tuple(tools))
    return loaded


def contract_sha256(contracts: dict[str, DomainContract]) -> str:
    """Stable identity of an approved catalog; temporary health never enters it."""

    return sha256(
        canonical_json(
            {
                domain: {
                    tool.name: {
                        "schema_fingerprint": tool.schema_fingerprint,
                        "read_only": tool.read_only,
                    }
                    for tool in contract.tools
                }
                for domain, contract in contracts.items()
            }
        ).encode()
    ).hexdigest()


CONTRACTS = load_contracts()
# Derived for the runtime host fingerprint; unpinned domains stay absent so a
# half-reviewed catalog can never be mounted.
APPROVED_SCHEMAS: dict[str, dict[str, str]] = {
    domain: contract.approved
    for domain, contract in CONTRACTS.items()
    if contract.verified
}
CONTRACT_SHA256 = contract_sha256(CONTRACTS)


__all__ = [
    "APPROVED_DOMAINS",
    "APPROVED_SCHEMAS",
    "CONTRACTS",
    "CONTRACT_SHA256",
    "DomainContract",
    "contract_sha256",
    "SNAPSHOT_PATH",
    "ToolContract",
    "load_contracts",
]
