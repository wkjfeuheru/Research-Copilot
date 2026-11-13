"""Canonical JSON and tool-schema fingerprints."""

import hashlib
import json
from typing import Any, Mapping


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def schema_fingerprint(schema: Mapping[str, Any] | None) -> str | None:
    if schema is None:
        return None
    return hashlib.sha256(canonical_json(schema).encode("utf-8")).hexdigest()
