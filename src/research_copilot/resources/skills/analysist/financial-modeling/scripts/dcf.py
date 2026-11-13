"""Year-end FCFF DCF. Forecast construction is the analyst's explicit assumption."""

from decimal import localcontext
from io_contract import cli, decimal, positive, text, comparable, number


def calculate(*, fcff, wacc, terminal_growth, net_debt, diluted_shares):
    """Context-checking entry. The numeric kernel is also available for composed code."""
    if not fcff:
        raise ValueError("Explicit annual forecast sequence required")
    comparable([*fcff, net_debt], ("entity", "unit", "currency"))
    comparable([net_debt, diluted_shares], ("entity", "currency", "period"))
    if diluted_shares["context"]["unit"] != "shares":
        raise ValueError("Diluted share count must use shares")
    base = net_debt["context"]["period"]
    if not base.isdigit() or len(base) != 4:
        raise ValueError("Explicit valuation year required")
    for year, flow in enumerate(fcff, 1):
        if (
            flow["context"]["period"] != str(int(base) + year)
            or flow["context"]["basis"] != "forecast"
        ):
            raise ValueError(
                "Contiguous annual FCFF forecasts after the valuation year required"
            )
    return fcff_dcf(
        fcff=[number(flow) for flow in fcff],
        wacc=wacc,
        terminal_growth=terminal_growth,
        net_debt=number(net_debt),
        diluted_shares=number(diluted_shares),
    )


def fcff_dcf(*, fcff, wacc, terminal_growth, net_debt, diluted_shares):
    if not fcff or len(fcff) > 100:
        raise ValueError("One to 100 explicit annual FCFF forecasts required")
    flows = [decimal(value) for value in fcff]
    w, g = decimal(wacc), decimal(terminal_growth)
    debt, shares = decimal(net_debt), positive(diluted_shares, "diluted shares")
    if w <= g or w <= -1 or g <= -1:
        raise ValueError("WACC must exceed perpetual growth; both must exceed -100%")
    with localcontext() as ctx:
        ctx.prec = 28
        pv = sum(flow / (1 + w) ** year for year, flow in enumerate(flows, 1))
        terminal = flows[-1] * (1 + g) / (w - g)
        discounted = terminal / (1 + w) ** len(flows)
        enterprise = pv + discounted
        return {
            key: text(value)
            for key, value in {
                "pv_fcff": pv,
                "terminal_value": terminal,
                "pv_terminal": discounted,
                "enterprise_value": enterprise,
                "equity_value": enterprise - debt,
                "per_share_value": (enterprise - debt) / shares,
            }.items()
        }


if __name__ == "__main__":
    cli(fcff_dcf)
