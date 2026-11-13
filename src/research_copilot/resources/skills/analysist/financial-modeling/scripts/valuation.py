"""PE/PB/PS/EV-EBITDA with explicit TTM and denomination checks."""

from decimal import localcontext
from io_contract import cli, comparable, number, positive, scalar

INPUTS = {
    "pe": ("price", "eps_ttm"),
    "pb": ("price", "bvps"),
    "ps": ("market_cap", "revenue_ttm"),
    "ev_ebitda": ("market_cap", "interest_bearing_debt", "cash", "ebitda_ttm"),
}


def calculate(kind, inputs):
    if kind not in INPUTS or set(inputs) != set(INPUTS[kind]):
        raise ValueError("Missing or unexpected valuation inputs")
    items = [inputs[k] for k in INPUTS[kind]]
    comparable(items, ("entity", "unit", "currency", "period"))
    for key in inputs:
        if "ttm" in key and inputs[key]["context"]["basis"] not in {
            "ttm",
            "consolidated_ttm",
        }:
            raise ValueError("Verified TTM basis required")
    with localcontext() as ctx:
        ctx.prec = 28
        a = [number(item) for item in items]
        if kind == "ev_ebitda":
            if a[1] < 0 or a[2] < 0:
                raise ValueError("Debt and cash cannot be negative")
            result = (a[0] + a[1] - a[2]) / positive(a[3])
        else:
            result = a[0] / positive(a[1])
        return scalar(result, items[0]["context"], "x")


if __name__ == "__main__":
    cli(calculate)
