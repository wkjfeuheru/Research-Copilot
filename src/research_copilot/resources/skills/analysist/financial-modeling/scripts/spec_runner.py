"""Host-frozen declarative metric computation (schema_version=2 result).

The host writes `spec_inputs.json` into the task directory (authorized fact
InputRefs, declared analyst assumptions, ordered MetricSpec entries) and runs
this fixed script inside the sandbox. Only declared binary-formula arithmetic
happens here; the host re-validates every value and semantic field at
registration, so any drift becomes an invalid_result with diagnostics.
Model-authored code is not involved in the standard metric path.
"""

import json
import os
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

SUPPORTED_FORMULAS = ("difference", "product", "ratio", "growth_percent")


def number(value):
    return Decimal(str(value).replace(",", "").removesuffix("%"))


def compute_expected(formula_id, a, b):
    if formula_id == "difference":
        return a - b
    if formula_id == "product":
        return a * b
    if formula_id == "ratio":
        return a / b
    if formula_id == "growth_percent":
        return (a / b - 1) * 100
    raise ValueError(f"Unsupported formula_id: {formula_id}")


def operand_value(operands, computed, key):
    if key in operands:
        item = operands[key]
        return number(item.get("raw_value", item.get("value")))
    if key in computed:
        return computed[key]["decimal"]
    raise ValueError(f"Unknown metric input: {key}")


def operand_is_estimate(operands, computed, key):
    if key in operands:
        item = operands[key]
        return bool(item.get("is_estimate") or "assumption_key" in item)
    if key in computed:
        return bool(computed[key]["is_estimate"])
    return False


def main(argv):
    source = argv[1] if len(argv) > 1 else "spec_inputs.json"
    with open(source, encoding="utf-8") as stream:
        document = json.load(stream)
    if document.get("schema_version") != 1:
        raise ValueError("spec_inputs.json requires schema_version=1")
    fact_refs = document["inputs"]
    assumptions = document["assumptions"]
    specs = document["metrics"]
    operands = dict(fact_refs)
    for key, value in assumptions.items():
        operands[key] = {"assumption_key": key, "is_estimate": True, **value}
    declared = set(operands) | {spec["key"] for spec in specs}
    computed, entries = {}, []
    for spec in specs:
        key = spec["key"]
        if key in operands or key in computed:
            raise ValueError(f"Duplicate metric key: {key}")
        if spec["formula_id"] not in SUPPORTED_FORMULAS:
            raise ValueError(f"Unsupported formula_id: {spec['formula_id']}")
        unknown = [k for k in spec["input_keys"] if k not in declared]
        if unknown:
            raise ValueError(f"Metric {key} references unknown inputs: {unknown}")
        if len(spec["input_keys"]) != 2:
            raise ValueError("Binary formula requires two ordered inputs")
        try:
            a = operand_value(operands, computed, spec["input_keys"][0])
            b = operand_value(operands, computed, spec["input_keys"][1])
            expected = compute_expected(spec["formula_id"], a, b)
            if spec["formula_id"] == "ratio" and spec["unit"] in {"%", "％"}:
                # Ratios declared in percent are registered as percentages.
                expected *= 100
        except (InvalidOperation, ZeroDivisionError) as exc:
            raise ValueError(f"Metric {key}: invalid formula inputs ({exc})") from exc
        quantum = Decimal(1).scaleb(-spec["display_precision"])
        value = expected.quantize(quantum, rounding=ROUND_HALF_UP)
        estimate = bool(
            spec.get("is_estimate")
            or spec["semantic_type"] == "inferred_amount"
            or any(
                operand_is_estimate(operands, computed, k) for k in spec["input_keys"]
            )
        )
        entry = {
            "key": key,
            "value": str(value),
            "unit": spec["unit"],
            "input_keys": list(spec["input_keys"]),
            "description": spec.get("description", ""),
            "context": {},
            "semantic_type": spec["semantic_type"],
            "formula_id": spec["formula_id"],
            "numerator_basis": spec["numerator_basis"],
            "denominator_basis": spec["denominator_basis"],
            "is_estimate": estimate,
            "display_precision": spec["display_precision"],
            "comparison": spec.get("comparison"),
        }
        entries.append(entry)
        # display_precision affects presentation only; never round chain operands.
        computed[key] = {"decimal": expected, "is_estimate": estimate}
    spec_index = {spec["key"]: spec for spec in specs}
    used, stack = set(), [k for spec in specs for k in spec["input_keys"]]
    while stack:
        item = stack.pop()
        if item in used:
            continue
        used.add(item)
        if item in spec_index:
            stack.extend(spec_index[item]["input_keys"])
    used_facts = [fact_refs[k] for k in sorted(used) if k in fact_refs]
    used_assumptions = {k: assumptions[k] for k in sorted(used) if k in assumptions}
    result = {
        "schema_version": 2,
        "input_refs": used_facts,
        "metrics": entries,
        "charts": [],
        "assumptions": used_assumptions,
        "checks": [
            "Declarative spec computation by the host-frozen runner; "
            "values and semantics are re-validated at registration"
        ],
        "limitations": [],
    }
    output_dir = os.environ.get("RC_OUTPUT_DIR", "/outputs")
    os.makedirs(output_dir, exist_ok=True)
    target = os.path.join(output_dir, "result.json")
    with open(target, "w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(
        f"metrics: {len(entries)} input_refs: {len(used_facts)} "
        f"assumptions: {len(used_assumptions)} out: {target}"
    )


if __name__ == "__main__":
    try:
        main(sys.argv)
    except (ValueError, InvalidOperation, KeyError, OSError) as exc:
        print(f"spec_runner failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
