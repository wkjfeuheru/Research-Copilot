"""Five canonical ratios; missing/zero/negative denominators are errors."""

from decimal import localcontext
from io_contract import cli, comparable, number, positive, scalar
from growth import endpoint

INPUTS = {
    "gross_margin": ("revenue", "cost"),
    "net_margin": ("net_profit", "revenue"),
    "roe": ("net_profit", "begin_equity", "end_equity"),
    "roa": ("net_profit", "begin_assets", "end_assets"),
    "debt_ratio": ("total_liabilities", "total_assets"),
}


def calculate(kind, inputs):
    if kind not in INPUTS or set(inputs) != set(INPUTS[kind]):
        raise ValueError("Missing or unexpected ratio inputs")
    items = [inputs[k] for k in INPUTS[kind]]
    comparable(items, ("entity", "unit", "currency", "basis"))
    periods = [endpoint(item["context"]["period"]) for item in items]
    if kind in {"roe", "roa"}:
        if (
            periods[0] != periods[2]
            or (periods[0][0] - periods[1][0]) * 4 + periods[0][1] - periods[1][1] != 4
        ):
            raise ValueError("Annual profit and beginning/end annual balances required")
    elif len(set(periods)) != 1:
        raise ValueError("Ratio periods differ")
    with localcontext() as ctx:
        ctx.prec = 28
        a = [number(item) for item in items]
        if kind == "gross_margin":
            result = (a[0] - a[1]) / positive(a[0])
        elif kind in {"roe", "roa"}:
            result = a[0] / positive((a[1] + a[2]) / 2)
        else:
            result = a[0] / positive(a[1])
        return scalar(result, items[0]["context"])


if __name__ == "__main__":
    cli(calculate)
