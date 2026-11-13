"""Frozen source-contract identity for report continuity.

Temporary domain health is deliberately excluded: only the approved catalog,
schema fingerprints and normalization version decide whether an existing
report may keep writing under the current host.
"""

from __future__ import annotations

from typing import Mapping


def source_contract_compatible(state, expected: Mapping[str, object]) -> bool:
    if not expected:
        return True
    manifest = state.role_data.get("runtime_manifest")
    if not isinstance(manifest, dict):
        # No dispatch has frozen a contract yet: this is a new report.
        return True
    return manifest.get("source_contract") == dict(expected)
