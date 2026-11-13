"""Pure skill scripts: independent known answers and legacy equivalence."""

from decimal import Decimal as D
import importlib
from pathlib import Path
import subprocess
import sys
import json
import pytest

SCRIPTS = (
    Path(__file__).parents[2]
    / "src/research_copilot/resources/skills/analysist/financial-modeling/scripts"
)


def script(name):
    assert (SCRIPTS / f"{name}.py").is_file(), (
        f"Missing standalone skill script: {name}"
    )
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(SCRIPTS))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.pop(0)


def value(amount, period="2025", **changes):
    return {
        "value": amount,
        "context": {
            "entity": "AAA",
            "unit": "CNY",
            "currency": "CNY",
            "period": period,
            "basis": "consolidated",
            "scale": "1",
            **changes,
        },
    }


def test_growth_and_cagr_match_known_answers():
    s = script("growth")
    assert s.calculate("yoy", value("120"), value("100", "2024"))["value"] == "0.2"
    assert D(
        s.calculate("cagr", value("121", "2026"), value("100", "2024"))["value"]
    ) == D("0.1")


@pytest.mark.parametrize("bad", ["0", "-1", "NaN", None])
def test_growth_rejects_invalid_base(bad):
    with pytest.raises(ValueError):
        script("growth").calculate("yoy", value("120"), value(bad, "2024"))


@pytest.mark.parametrize(
    "change", [{"unit": "USD"}, {"entity": "BBB"}, {"basis": "ttm"}, {"period": "2023"}]
)
def test_growth_rejects_incomparable_context(change):
    base = value("100", "2024")
    base["context"].update(change)
    with pytest.raises(ValueError):
        script("growth").calculate("yoy", value("120"), base)


@pytest.mark.parametrize(
    "kind,data",
    [
        (
            "bar",
            {
                "labels": ["A", "B"],
                "series": [{"name": "Revenue", "values": ["1", "-2"]}],
            },
        ),
        (
            "line",
            {
                "labels": ["A", "B"],
                "series": [{"name": "Revenue", "values": ["1", None]}],
            },
        ),
        ("scatter", {"points": [["1", "2"], ["3", "4"]]}),
        ("waterfall", {"labels": ["A", "B"], "values": ["5", "-2"]}),
        (
            "sensitivity_heatmap",
            {"x": ["0", "0.02"], "y": ["0.1"], "values": [["100", "120"]]},
        ),
        ("valuation_range", {"labels": ["DCF"], "low": ["90"], "high": ["110"]}),
        (
            "stacked_bar",
            {
                "labels": ["A"],
                "series": [
                    {"name": "a", "values": ["1"]},
                    {"name": "b", "values": ["2"]},
                ],
            },
        ),
        (
            "candlestick",
            {
                "labels": ["2025-01-01"],
                "ohlc": [["1", "2", "0.5", "3"]],
                "volume": ["100"],
                "input_keys": ["price"],
            },
        ),
        ("treemap", {"nodes": [{"name": "a", "value": "1"}]}),
        (
            "sankey",
            {
                "nodes": [{"name": "a"}, {"name": "b"}],
                "links": [{"source": "a", "target": "b", "value": "10"}],
                "flow_basis": "authorized cashflow",
            },
        ),
    ],
)
def test_reference_charts_produce_data_options_and_png(kind, data, tmp_path):
    chart = script("charts").render(
        kind=kind,
        title="Research",
        unit="CNY",
        data=data,
        output_dir=str(tmp_path),
        engine="matplotlib",
    )
    assert (
        (tmp_path / chart["static_file"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    )
    assert json.loads((tmp_path / chart["data_file"]).read_text()) == data
    assert script("echarts_options").build(
        kind, title="Research", unit="CNY", data=data
    )["series"]


def test_sankey_rejects_unfounded_flow():
    with pytest.raises(ValueError):
        script("echarts_options").build(
            "sankey",
            title="bad",
            unit="CNY",
            data={"nodes": [{"name": "a"}], "links": []},
        )


@pytest.mark.parametrize(
    "kind,inputs,expected",
    [
        ("gross_margin", {"revenue": "100", "cost": "60"}, "0.4"),
        ("net_margin", {"net_profit": "15", "revenue": "100"}, "0.15"),
        (
            "roe",
            {"net_profit": "15", "begin_equity": "90", "end_equity": "110"},
            "0.15",
        ),
        (
            "roa",
            {"net_profit": "15", "begin_assets": "190", "end_assets": "210"},
            "0.075",
        ),
        ("debt_ratio", {"total_liabilities": "40", "total_assets": "100"}, "0.4"),
    ],
)
def test_ratio_known_answers(kind, inputs, expected):
    data = {
        k: value(v, "2024" if k.startswith("begin_") else "2025")
        for k, v in inputs.items()
    }
    assert script("ratios").calculate(kind, data)["value"] == expected


@pytest.mark.parametrize(
    "kind,inputs,expected",
    [
        ("pe", {"price": "20", "eps_ttm": "2"}, "10"),
        ("pb", {"price": "20", "bvps": "5"}, "4"),
        ("ps", {"market_cap": "100", "revenue_ttm": "50"}, "2"),
        (
            "ev_ebitda",
            {
                "market_cap": "100",
                "interest_bearing_debt": "30",
                "cash": "10",
                "ebitda_ttm": "20",
            },
            "6",
        ),
    ],
)
def test_valuation_known_answers(kind, inputs, expected):
    data = {
        k: value(v, basis="ttm" if "ttm" in k else "consolidated")
        for k, v in inputs.items()
    }
    assert script("valuation").calculate(kind, data)["value"] == expected


def test_period_change_checks_flow_and_quarters():
    s = script("growth")
    a = value(
        "30",
        "2025H1",
        basis="consolidated_cumulative",
        measure="revenue",
        accounting_basis="original",
    )
    b = value(
        "10",
        "2025Q1",
        basis="consolidated_cumulative",
        measure="revenue",
        accounting_basis="original",
    )
    assert s.period_change("single_quarter", a, b)["value"] == "20"
    b["context"]["period"] = "2024Q4"
    with pytest.raises(ValueError):
        s.period_change("single_quarter", a, b)


def test_dcf_hand_calculated_single_year():
    r = script("dcf").fcff_dcf(
        fcff=[D("110")],
        wacc=D("0.1"),
        terminal_growth=D("0"),
        net_debt=D("100"),
        diluted_shares=D("10"),
    )
    assert r == {
        "pv_fcff": "100",
        "terminal_value": "1100",
        "pv_terminal": "1000",
        "enterprise_value": "1100",
        "equity_value": "1000",
        "per_share_value": "100",
    }


@pytest.mark.parametrize(
    "wacc,g,shares", [("0.02", "0.02", "10"), ("0.01", "0.02", "10"), ("0.1", "0", "0")]
)
def test_dcf_rejects_invalid_assumptions(wacc, g, shares):
    with pytest.raises(ValueError):
        script("dcf").fcff_dcf(
            fcff=[D("110")],
            wacc=D(wacc),
            terminal_growth=D(g),
            net_debt=D("0"),
            diluted_shares=D(shares),
        )


def test_sensitivity_matches_pointwise_dcf():
    s = script("sensitivity").calculate(
        fcff=["110"],
        waccs=["0.1", "0.2"],
        growth_rates=["0", "0.02"],
        net_debt="100",
        diluted_shares="10",
    )
    for i, w in enumerate(["0.1", "0.2"]):
        for j, g in enumerate(["0", "0.02"]):
            expected = script("dcf").fcff_dcf(
                fcff=[D("110")],
                wacc=D(w),
                terminal_growth=D(g),
                net_debt=D("100"),
                diluted_shares=D("10"),
            )["per_share_value"]
            assert s["values"][i][j] == expected


def test_scripts_have_real_cli(tmp_path):
    s = script("growth")
    p = tmp_path / "input.json"
    p.write_text(
        json.dumps(
            {"method": "yoy", "current": value("120"), "base": value("100", "2024")}
        )
    )
    output = subprocess.run(
        [sys.executable, "-B", str(Path(s.__file__)), str(p)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(output.stdout)["value"] == "0.2"


def test_standalone_results_match_published_formulas():
    def gross_margin(revenue, cost):
        return (revenue - cost) / revenue

    def net_margin(net_profit, revenue):
        return net_profit / revenue

    def roe(net_profit, begin_equity, end_equity):
        return net_profit / ((begin_equity + end_equity) / 2)

    def roa(net_profit, begin_assets, end_assets):
        return net_profit / ((begin_assets + end_assets) / 2)

    def debt_ratio(total_liabilities, total_assets):
        return total_liabilities / total_assets

    def pe(price, eps_ttm):
        return price / eps_ttm

    def pb(price, bvps):
        return price / bvps

    def ps(market_cap, revenue_ttm):
        return market_cap / revenue_ttm

    def ev_ebitda(market_cap, interest_bearing_debt, cash, ebitda_ttm):
        return (market_cap + interest_bearing_debt - cash) / ebitda_ttm

    cases = [
        (
            "ratios",
            "gross_margin",
            {"revenue": "143.31", "cost": "101.21"},
            gross_margin,
        ),
        (
            "ratios",
            "net_margin",
            {"net_profit": "-15.03", "revenue": "120.34"},
            net_margin,
        ),
        (
            "ratios",
            "roe",
            {"net_profit": "12.31", "begin_equity": "70.32", "end_equity": "81.23"},
            roe,
        ),
        (
            "ratios",
            "roa",
            {"net_profit": "15.31", "begin_assets": "201.23", "end_assets": "245.32"},
            roa,
        ),
        (
            "ratios",
            "debt_ratio",
            {"total_liabilities": "83.21", "total_assets": "235.21"},
            debt_ratio,
        ),
        ("valuation", "pe", {"price": "23.51", "eps_ttm": "1.28"}, pe),
        ("valuation", "pb", {"price": "23.51", "bvps": "5.32"}, pb),
        ("valuation", "ps", {"market_cap": "124.21", "revenue_ttm": "72.32"}, ps),
        (
            "valuation",
            "ev_ebitda",
            {
                "market_cap": "124.21",
                "interest_bearing_debt": "32.17",
                "cash": "18.31",
                "ebitda_ttm": "22.31",
            },
            ev_ebitda,
        ),
    ]
    for module, kind, inputs, reference in cases:
        facts = {
            k: value(
                v,
                "2024" if k.startswith("begin_") else "2025",
                basis="ttm" if "ttm" in k else "consolidated",
            )
            for k, v in inputs.items()
        }
        result = script(module).calculate(kind, facts)
        assert D(result["value"]) == reference(
            **{k: D(v) for k, v in inputs.items()}
        ), (
            module,
            kind,
        )


def test_comparison_sorts_without_dropping_context():
    result = script("comparison").calculate(
        companies=[{"entity": "AAA"}, {"entity": "BBB"}],
        metrics=[
            {
                "name": "Revenue",
                "values": [
                    {"entity": "AAA", "value": value("10")},
                    {"entity": "BBB", "value": value("20", entity="BBB")},
                ],
            }
        ],
        sort_by="Revenue",
    )
    assert result["entities"] == ["BBB", "AAA"]
    assert result["rows"][0]["values"] == {"AAA": "10", "BBB": "20"}


def test_financial_boundaries_do_not_mint_zero():
    for module, kind, inputs in [
        ("ratios", "gross_margin", {"revenue": "0", "cost": "2"}),
        ("valuation", "pe", {"price": "10", "eps_ttm": "-1"}),
    ]:
        facts = {
            k: value(v, basis="ttm" if "ttm" in k else "consolidated")
            for k, v in inputs.items()
        }
        with pytest.raises(ValueError):
            script(module).calculate(kind, facts)


def test_dcf_context_checks_currency_and_forecast_periods():
    s = script("dcf")
    assert hasattr(s, "calculate"), (
        "DCF needs a context-checking entry alongside the pure numeric kernel"
    )
    inputs = {
        "fcff": [value("110", "2026", basis="forecast")],
        "wacc": "0.1",
        "terminal_growth": "0",
        "net_debt": value("100", "2025"),
        "diluted_shares": value("10", "2025", unit="shares"),
    }
    assert s.calculate(**inputs)["per_share_value"] == "100"
    inputs["fcff"][0]["context"]["currency"] = "USD"
    with pytest.raises(ValueError, match="currency"):
        s.calculate(**inputs)
