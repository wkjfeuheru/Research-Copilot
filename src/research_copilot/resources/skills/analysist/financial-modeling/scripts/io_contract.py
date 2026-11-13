"""Standalone Decimal/context utilities; no application or database dependencies."""

from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_EVEN
import json
from pathlib import Path
import sys


def decimal(value):
    if value is None or isinstance(value, (bool, float)):
        raise ValueError("Use an explicit decimal string, not a missing value or float")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid decimal") from exc
    if not result.is_finite():
        raise ValueError("Finite decimal required")
    return result


def text(value):
    value = decimal(value)
    return (
        format(value, "f").rstrip("0").rstrip(".")
        if "." in format(value, "f")
        else format(value, "f")
    )


def precision():
    ctx = localcontext()
    return ctx


def number(item):
    context = item.get("context", {})
    for key in ("entity", "unit", "currency", "period", "basis", "scale"):
        if not isinstance(context.get(key), str) or not context[key].strip():
            raise ValueError("Missing context: " + key)
    scale = decimal(context["scale"])
    if scale <= 0:
        raise ValueError("Scale must be positive")
    with localcontext() as ctx:
        ctx.prec, ctx.rounding = 28, ROUND_HALF_EVEN
        return decimal(item["value"]) * scale


def comparable(items, dimensions=("entity", "unit", "currency", "period", "basis")):
    if not items:
        raise ValueError("Missing inputs")
    for item in items:
        number(item)
        for key in dimensions:
            if item["context"].get(key) != items[0]["context"].get(key):
                raise ValueError("Incomparable context: " + key)


def positive(value, name="denominator"):
    value = decimal(value)
    if value <= 0:
        raise ValueError(name + " must be positive")
    return value


def scalar(value, context, unit="ratio"):
    return {
        "value": text(value),
        "unit": unit,
        "context": {**context, "unit": unit, "scale": "1"},
    }


def write_result(
    path,
    *,
    metrics,
    input_refs,
    assumptions=None,
    charts=None,
    checks=None,
    limitations=None,
):
    """Write a candidate manifest. Only the host can issue formal metric/chart IDs."""
    result = {
        "schema_version": 2,
        "metrics": metrics,
        "charts": charts or [],
        "input_refs": input_refs,
        "assumptions": assumptions or {},
        "checks": checks or [],
        "limitations": limitations or [],
    }
    Path(path).write_text(
        json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2),
        encoding="utf-8",
    )
    return result


def cli(function):
    data = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    result = function(**data)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(encoded, encoding="utf-8")
    else:
        print(encoded)
