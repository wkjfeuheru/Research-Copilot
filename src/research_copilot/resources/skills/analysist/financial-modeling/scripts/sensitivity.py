"""Explicit WACC x perpetual-growth grid; invalid points fail, never become zero."""

from dcf import fcff_dcf
from io_contract import cli, decimal, text


def calculate(*, fcff, waccs, growth_rates, net_debt, diluted_shares):
    if not waccs or not growth_rates or len(waccs) * len(growth_rates) > 2500:
        raise ValueError("A bounded nonempty grid is required")
    return {
        "waccs": [text(decimal(v)) for v in waccs],
        "growth_rates": [text(decimal(v)) for v in growth_rates],
        "values": [
            [
                fcff_dcf(
                    fcff=fcff,
                    wacc=w,
                    terminal_growth=g,
                    net_debt=net_debt,
                    diluted_shares=diluted_shares,
                )["per_share_value"]
                for g in growth_rates
            ]
            for w in waccs
        ],
    }


if __name__ == "__main__":
    cli(calculate)
