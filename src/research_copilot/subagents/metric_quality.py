"""Deterministic checks for declared formulas, not a proof of arbitrary economics."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def number(value):
    return Decimal(str(value).replace(",", "").removesuffix("%"))


SUPPORTED_FORMULAS = ("difference", "product", "ratio", "growth_percent")

_SEMANTIC_SYNONYMS = {
    "百分点变动": "rate_difference",
    "百分点变化": "rate_difference",
    "百分点差": "rate_difference",
    "pp变动": "rate_difference",
    "pp差": "rate_difference",
}


def canonical_semantic(value):
    text = str(value).strip()
    return _SEMANTIC_SYNONYMS.get(text, text)


def canonical_unit(value):
    text = str(value).strip()
    return "pp" if text in {"百分点", "pp"} else text


def compute_expected(formula_id, a, b):
    """Shared declaration-time arithmetic; the sandbox spec runner mirrors this."""
    if formula_id == "difference":
        return a - b
    if formula_id == "product":
        return a * b
    if formula_id == "ratio":
        return a / b
    if formula_id == "growth_percent":
        return (a / b - 1) * 100
    raise ValueError(f"Unsupported formula_id: {formula_id}")


def preflight_specs(specs, inputs):
    """Validate a declarative proposal before spending an execution, without issuing receipts."""
    operands = dict(inputs)
    for spec in specs:
        if len(spec["input_keys"]) != 2:
            raise ValueError("Binary formula requires two ordered inputs")
        a, b = [
            number(
                operands[k].get("calculated_value")
                or operands[k].get("raw_value", operands[k].get("value"))
            )
            for k in spec["input_keys"]
        ]
        value = compute_expected(spec["formula_id"], a, b)
        if spec["formula_id"] == "ratio" and spec["unit"] in {"%", "％"}:
            value *= 100
        checked = validate_metric_semantics({**spec, "value": str(value)}, operands)
        checked["inputs"] = [
            leaf
            for k in spec["input_keys"]
            for leaf in operands[k].get("inputs", [operands[k]])
        ]
        operands[spec["key"]] = checked


def validate_metric_semantics(metric, inputs):
    required = (
        "semantic_type",
        "formula_id",
        "numerator_basis",
        "denominator_basis",
        "display_precision",
        "is_estimate",
    )
    if any(k not in metric or metric[k] is None for k in required):
        raise ValueError("Metric semantic metadata is required")
    key = metric.get("key", metric.get("local_key", ""))
    kind = canonical_semantic(metric["semantic_type"])
    if key.startswith("net_margin_impact") or key in {
        "net_margin_residual_pp",
        "net_margin_decomposition_residual_pp",
        "net_margin_impact_aftertax_pp",
    }:
        raise ValueError("Use rate_difference; unsupported net margin attribution")
    if kind in {"net_margin_contribution", "tax_adjusted_attribution"}:
        raise ValueError(
            "Unsupported attribution: use rate_difference or supply a separately reviewed full bridge"
        )
    if kind in {"rate_difference", "growth_gap"} and metric["unit"] not in {
        "pp",
        "百分点",
    }:
        raise ValueError("Rate differences require percentage points")
    if kind == "rate_difference" and "净利率影响" in metric.get("description", ""):
        raise ValueError("rate_difference is not net margin impact")
    keys = metric["input_keys"]
    if not set(keys) <= inputs.keys():
        raise ValueError("Unknown metric input")
    derived = dict(metric)
    # Untrusted manifests cannot supply the host's recomputation cache.
    derived.pop("calculated_value", None)
    from research_copilot.subagents.comparison import validate_comparison

    derived["comparison"] = validate_comparison(metric, inputs)
    derived["is_estimate"] = bool(
        metric["is_estimate"]
        or kind == "inferred_amount"
        or any(
            inputs[k].get("is_estimate") or "assumption_key" in inputs[k] for k in keys
        )
    )
    formula = metric["formula_id"]
    if formula in SUPPORTED_FORMULAS:
        if len(keys) != 2:
            raise ValueError("Binary formula requires two ordered inputs")
        try:
            a, b = [
                number(
                    inputs[k].get("calculated_value")
                    or inputs[k].get("raw_value", inputs[k].get("value"))
                )
                for k in keys
            ]
            expected = compute_expected(formula, a, b)
            percent_ratio = formula == "ratio" and metric["unit"] in {"%", "％"}
            if percent_ratio:
                # A ratio declared in percent (for example a net margin) is
                # registered as 14.02, not as the underlying fraction 0.14.
                expected *= 100
            derived["calculated_value"] = str(expected)
            # Propagate half-unit precision from original decimal strings; derived
            # operands carry their already propagated bound instead of re-rounding.
            ea, eb = [
                number(inputs[k]["rounding_error"])
                if inputs[k].get("rounding_error") is not None
                else Decimal("0.5").scaleb(
                    number(inputs[k].get("raw_value", inputs[k].get("value")))
                    .as_tuple()
                    .exponent
                )
                for k in keys
            ]
            if formula == "difference":
                error = ea + eb
            elif formula == "product":
                error = abs(a) * eb + abs(b) * ea + ea * eb
            elif abs(b) > eb:
                error = (ea * abs(b) + abs(a) * eb) / (abs(b) * (abs(b) - eb))
                if formula == "growth_percent" or percent_ratio:
                    error *= 100
            else:
                raise ValueError("Input precision permits a zero denominator")
            derived["rounding_error"] = str(error)
            quantum = Decimal(1).scaleb(-metric["display_precision"])
            if number(metric["value"]).quantize(
                quantum, rounding=ROUND_HALF_UP
            ) != expected.quantize(quantum, rounding=ROUND_HALF_UP):
                raise ValueError(
                    f"Metric formula/value mismatch: {key}, formula={formula}, "
                    f"actual={metric['value']}, expected={expected}, precision={metric['display_precision']}"
                )
        except (InvalidOperation, ZeroDivisionError) as exc:
            raise ValueError("Invalid formula inputs") from exc
    if "margin" in key.casefold() and metric["unit"] in {"%", "％"}:
        try:
            value = number(metric["value"])
        except InvalidOperation as exc:
            raise ValueError("Invalid margin value") from exc
        if value < -100 or value > 100:
            raise ValueError(
                f"{key}: a margin percentage must be within -100..100; compute "
                "(revenue - operating_cost) / revenue with difference then ratio"
            )
    return derived


def validate_metric_batch(
    metrics, inputs, *, requirements=(), require_fact_lineage=False
):
    operands = dict(inputs)
    validated, errors = {}, []
    for metric in metrics:
        key = metric["key"]
        lineage = [
            leaf
            for k in metric["input_keys"]
            if k in operands
            for leaf in operands[k].get("inputs", [operands[k]])
        ]
        missing_fact = require_fact_lineage and not any(
            i.get("evidence_id") for i in lineage
        )
        if missing_fact:
            errors.append(f"{key}: Metric requires at least one authorized fact input")
        try:
            item = validate_metric_semantics(metric, operands)
            if missing_fact:
                continue
            item["inputs"] = lineage
            validated[key] = item
            operands[key] = item
        except (ValueError, InvalidOperation, ZeroDivisionError) as exc:
            errors.append(f"{key}: {exc}")
    for expected in requirements:

        def matches(metric):
            if canonical_semantic(metric["semantic_type"]) != canonical_semantic(
                expected["semantic_type"]
            ) or canonical_unit(metric["unit"]) != canonical_unit(expected["unit"]):
                return False
            # TaskBrief requirements carry semantic_type/unit only; frozen
            # regression criteria additionally pin the exact value.
            if expected.get("value") in (None, ""):
                return True
            return number(metric["value"]) == number(expected["value"])

        if not any(matches(m) for m in validated.values()):
            errors.append(
                f"Missing required metric: semantic_type={expected['semantic_type']}, "
                f"unit={expected['unit']}"
                + (f", value={expected['value']}" if expected.get("value") else "")
                + ". Use the frozen metric definitions."
            )
    if errors:
        raise ValueError("\n".join(errors))
    return validated


def rounding_error(value, decimal_places, multiplier="1"):
    """Maximum half-unit rounding error propagated through a multiplication."""
    return str(
        Decimal("0.5") * Decimal(1).scaleb(-decimal_places) * abs(number(multiplier))
    )


def validate_registered_metrics(metrics, selected):
    """Rebuild selected dependency chains from original operands, ignoring cached values."""
    visited, visiting = set(), set()
    for ident in selected:
        if ident not in metrics:
            raise ValueError("Unknown registered metric")

    def check(ident):
        if ident in visited:
            return
        if ident in visiting:
            raise ValueError("Cyclic registered calculation")
        metric = metrics[ident]
        # Historical receipts without declarative inputs stay readable; no new
        # arithmetic is inferred from free-text descriptions.
        if "input_keys" not in metric:
            return
        visiting.add(ident)
        operands = {
            row["key"]: row for row in metric.get("inputs", []) if row.get("key")
        }
        operands.update(
            {
                key: {"assumption_key": key, **value}
                for key, value in metric.get("assumptions", {}).items()
            }
        )
        for key in metric["input_keys"]:
            if key in operands:
                continue
            matches = [
                mid
                for mid, row in metrics.items()
                if row.get("execution_id") == metric.get("execution_id")
                and row.get("local_key", row.get("key")) == key
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Missing or ambiguous registered calculation dependency: " + key
                )
            check(matches[0])
            operands[key] = recomputed[matches[0]]
        recomputed[ident] = validate_metric_semantics(
            {**metric, "key": metric.get("local_key", metric.get("key", ident))},
            operands,
        )
        visiting.remove(ident)
        visited.add(ident)

    recomputed = {}
    for ident in selected:
        check(ident)
    return recomputed
