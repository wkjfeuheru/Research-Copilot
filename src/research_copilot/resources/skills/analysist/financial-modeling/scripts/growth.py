"""Growth and period changes with explicit comparability; Decimal only."""

from decimal import Decimal, localcontext
import re
from io_contract import cli, comparable, number, positive, scalar, decimal


def endpoint(period):
    match = re.fullmatch(r"((?:19|20)\d{2})(?:Q([1-4])|H1)?", period)
    if not match:
        raise ValueError("Explicit annual/quarter period required")
    return int(match[1]), int(match[2] or (2 if period.endswith("H1") else 4))


def calculate(method, current, base, years=None):
    comparable([current, base], ("entity", "unit", "currency", "basis"))
    cy, cq = endpoint(current["context"]["period"])
    by, bq = endpoint(base["context"]["period"])
    span = Decimal((cy - by) * 4 + cq - bq) / 4
    a, b = number(current), positive(number(base), "base")
    with localcontext() as ctx:
        ctx.prec = 28
        if method == "yoy":
            if cy - by != 1 or cq != bq:
                raise ValueError("YoY requires matching periods one year apart")
            result = a / b - 1
        elif method == "qoq":
            if (
                span != Decimal("0.25")
                or "Q" not in current["context"]["period"]
                or "Q" not in base["context"]["period"]
                or any(
                    t in current["context"]["basis"].lower()
                    for t in ("cumulative", "ytd", "累计")
                )
            ):
                raise ValueError("QoQ requires adjacent noncumulative quarters")
            result = a / b - 1
        elif method == "cagr":
            positive(a, "current")
            positive(span, "years")
            if years is not None and decimal(years) != span:
                raise ValueError("Years differ from verified periods")
            result = ((a / b).ln() / span).exp() - 1
        else:
            raise ValueError("Unknown growth method")
        return scalar(result, current["context"])


def period_change(method, current, base=None):
    a = number(current)
    context = current["context"]
    cy, cq = endpoint(context["period"])
    if base is not None:
        comparable(
            [current, base],
            ("entity", "unit", "currency", "basis", "measure", "accounting_basis"),
        )
        by, bq = endpoint(base["context"]["period"])
    for key in ("measure", "accounting_basis"):
        if not context.get(key):
            raise ValueError("Missing context: " + key)
    if method == "single_quarter":
        if (
            context["basis"] != "consolidated_cumulative"
            or context["measure"]
            not in {"revenue", "cost", "operating_revenue", "operating_cost"}
            or context["unit"] in {"ratio", "shares", "x", "percent"}
        ):
            raise ValueError("Adjacent cumulative revenue/cost required")
        if (base is None and cq != 1) or (
            base is not None and (cy != by or cq - bq != 1)
        ):
            raise ValueError("Missing or nonadjacent cumulative period")
        return scalar(
            a - (number(base) if base else 0),
            {**context, "period": f"{cy}Q{cq}", "basis": "consolidated_single_quarter"},
            context["unit"],
        )
    if method == "percentage_points":
        if (
            base is None
            or context["unit"] != "ratio"
            or context["basis"] != "consolidated_single_quarter"
            or (cy - by) * 4 + cq - bq != 1
            or "Q" not in context["period"]
            or "Q" not in base["context"]["period"]
        ):
            raise ValueError("Adjacent single-quarter ratios required")
        return scalar((a - number(base)) * 100, context, "percentage_points")
    raise ValueError("Unknown period method")


if __name__ == "__main__":
    cli(calculate)
