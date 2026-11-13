"""Separate structural drift from annotations without trusting new tool text.

Only the shared envelope's domain caption may change without a new review.
Descriptions of units, dates, inputs and records remain part of the contract.
"""

from typing import Any

from research_copilot.sources.fingerprint import schema_fingerprint

_ANNOTATIONS = {"description", "title", "$comment", "examples"}
_SCHEMA_MAPS = {
    "properties",
    "patternProperties",
    "$defs",
    "definitions",
    "dependentSchemas",
}
_SCHEMA_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SCHEMA_VALUES = {
    "items",
    "additionalItems",
    "additionalProperties",
    "unevaluatedProperties",
    "unevaluatedItems",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
}
_DOMAIN_CAPTION = "/outputSchema/properties/domain/description"


def _structure(schema):
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key in _ANNOTATIONS:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            result[key] = {name: _structure(child) for name, child in value.items()}
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            result[key] = [_structure(child) for child in value]
        elif key in _SCHEMA_VALUES:
            result[key] = (
                [_structure(child) for child in value]
                if isinstance(value, list)
                else _structure(value)
            )
        else:
            # Values of const/enum/default and property names are data, not schemas.
            result[key] = value
    return result


def structural_fingerprint(input_schema, output_schema):
    return schema_fingerprint(
        {
            "inputSchema": _structure(input_schema),
            "outputSchema": _structure(output_schema),
        }
    )


def _changed_paths(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                result.append(pointer)
            else:
                result.extend(_changed_paths(before[key], after[key], pointer))
        return result
    if type(before) is type(after) and before == after:
        return []
    return [path]


def compare_schemas(
    reviewed_input, reviewed_output, actual_input, actual_output
) -> dict[str, Any]:
    before = {"inputSchema": reviewed_input, "outputSchema": reviewed_output}
    after = {"inputSchema": actual_input, "outputSchema": actual_output}
    paths = _changed_paths(before, after)
    old_structure = structural_fingerprint(reviewed_input, reviewed_output)
    new_structure = structural_fingerprint(actual_input, actual_output)
    caption = (reviewed_output or {}).get("properties", {}).get("domain", {})
    if not paths:
        classification = "identical"
    elif paths == [_DOMAIN_CAPTION] and caption.get("type") == "string":
        classification = "domain_caption"
    elif old_structure == new_structure:
        classification = "documentation_changed"
    else:
        classification = "structure_changed"
    return {
        "classification": classification,
        "compatible": classification in {"identical", "domain_caption"},
        "paths": paths,
        "reviewed_structure_sha256": old_structure,
        "observed_structure_sha256": new_structure,
    }
