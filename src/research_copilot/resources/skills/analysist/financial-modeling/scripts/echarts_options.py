"""JSON-only reference options. Values are presentation data, never new calculations."""

from io_contract import cli, decimal

KINDS = {
    "bar",
    "line",
    "scatter",
    "waterfall",
    "sensitivity_heatmap",
    "valuation_range",
    "stacked_bar",
    "candlestick",
    "treemap",
    "sankey",
}


def numeric(value):
    return None if value is None else float(decimal(value))


def build(kind, *, title, unit, data):
    if kind not in KINDS or not title or not unit:
        raise ValueError("Known kind, title and unit required")
    option = {
        "title": {"text": title},
        "animation": False,
        "textStyle": {"fontFamily": "Noto Sans CJK SC"},
        "tooltip": {"renderMode": "richText", "trigger": "item"},
        "legend": {},
        "grid": {
            "left": 90,
            "right": 50,
            "top": 90,
            "bottom": 90,
            "containLabel": True,
        },
        "xAxis": {"type": "category", "data": data.get("labels", [])},
        "yAxis": {"type": "value", "name": unit},
    }
    if kind in {"bar", "line", "stacked_bar"}:
        if not data.get("labels") or not data.get("series"):
            raise ValueError("Nonempty labels and series required")
        option["series"] = []
        for item in data["series"]:
            if len(item["values"]) != len(data["labels"]):
                raise ValueError("Series length mismatch")
            option["series"].append(
                {
                    "name": item["name"],
                    "type": "line" if kind == "line" else "bar",
                    "data": [numeric(v) for v in item["values"]],
                    **({"stack": "total"} if kind == "stacked_bar" else {}),
                }
            )
        option["dataZoom"] = [{"type": "inside"}, {"type": "slider"}]
    elif kind == "scatter":
        if not data.get("points") or any(len(p) != 2 for p in data["points"]):
            raise ValueError("Nonempty x,y points required")
        option["xAxis"] = {"type": "value"}
        option["series"] = [
            {
                "type": "scatter",
                "data": [[numeric(v) for v in p] for p in data["points"]],
            }
        ]
    elif kind == "waterfall":
        values = [numeric(v) for v in data["values"]]
        if not values or len(values) != len(data["labels"]):
            raise ValueError("Nonempty waterfall labels and values required")
        running, bases, spans = 0, [], []
        for value in values:
            bases.append(min(running, running + value))
            spans.append(abs(value))
            running += value
        option["series"] = [
            {
                "name": "offset",
                "type": "bar",
                "stack": "waterfall",
                "data": bases,
                "itemStyle": {"color": "transparent"},
                "silent": True,
            },
            {"name": unit, "type": "bar", "stack": "waterfall", "data": spans},
        ]
    elif kind == "sensitivity_heatmap":
        if (
            not data.get("x")
            or not data.get("y")
            or len(data["values"]) != len(data["y"])
            or any(len(row) != len(data["x"]) for row in data["values"])
        ):
            raise ValueError("Rectangular nonempty sensitivity grid required")
        cells = [
            [j, i, numeric(v)]
            for i, row in enumerate(data["values"])
            for j, v in enumerate(row)
        ]
        option.update(
            xAxis={"type": "category", "data": data["x"]},
            yAxis={"type": "category", "data": data["y"], "name": unit},
            visualMap={
                "min": min(c[2] for c in cells),
                "max": max(c[2] for c in cells),
                "calculable": False,
            },
            series=[{"type": "heatmap", "data": cells, "label": {"show": True}}],
        )
    elif kind == "valuation_range":
        lo, hi = [numeric(v) for v in data["low"]], [numeric(v) for v in data["high"]]
        if (
            not lo
            or len(lo) != len(hi)
            or len(lo) != len(data["labels"])
            or any(l > h for l, h in zip(lo, hi))
        ):
            raise ValueError("Valid low/high intervals required")
        option.update(
            xAxis={"type": "value", "name": unit},
            yAxis={"type": "category", "data": data["labels"]},
            series=[
                {
                    "name": "offset",
                    "type": "bar",
                    "stack": "range",
                    "data": lo,
                    "itemStyle": {"color": "transparent"},
                    "silent": True,
                },
                {
                    "name": "range",
                    "type": "bar",
                    "stack": "range",
                    "data": [h - l for l, h in zip(lo, hi)],
                },
            ],
        )
    elif kind == "candlestick":
        if (
            not data.get("input_keys")
            or not data.get("labels")
            or len(data["ohlc"]) != len(data["labels"])
            or len(data["volume"]) != len(data["labels"])
        ):
            raise ValueError("Authorized price input and aligned OHLC/volume required")
        candles = [[numeric(v) for v in row] for row in data["ohlc"]]
        if any(
            len(row) != 4 or row[2] > min(row[:2]) or row[3] < max(row[:2])
            for row in candles
        ) or any(numeric(v) < 0 for v in data["volume"]):
            raise ValueError("Invalid OHLC bounds or volume")
        option["grid"] = [
            {"left": 90, "right": 50, "top": 90, "height": "50%"},
            {"left": 90, "right": 50, "top": "73%", "height": "13%"},
        ]
        option["xAxis"] = [
            {"type": "category", "data": data["labels"]},
            {"type": "category", "data": data["labels"], "gridIndex": 1},
        ]
        option["yAxis"] = [
            {"type": "value", "name": unit, "scale": True},
            {"type": "value", "name": "Volume", "gridIndex": 1},
        ]
        option["series"] = [
            {"type": "candlestick", "data": candles},
            {
                "name": "Volume",
                "type": "bar",
                "data": [numeric(v) for v in data["volume"]],
                "xAxisIndex": 1,
                "yAxisIndex": 1,
            },
        ]
    elif kind == "treemap":
        nodes = [
            {"name": n["name"], "value": numeric(n["value"])} for n in data["nodes"]
        ]
        if not nodes or any(n["value"] <= 0 for n in nodes):
            raise ValueError("Positive tree weights required")
        option["series"] = [
            {"type": "treemap", "data": nodes, "roam": False, "nodeClick": False}
        ]
        option.pop("xAxis")
        option.pop("yAxis")
    else:
        if not data.get("flow_basis") or not data.get("links"):
            raise ValueError(
                "True source-target flows and documented flow_basis required"
            )
        names = {n["name"] for n in data["nodes"]}
        links = [{**link, "value": numeric(link["value"])} for link in data["links"]]
        if any(
            link["source"] not in names
            or link["target"] not in names
            or link["source"] == link["target"]
            or link["value"] <= 0
            for link in links
        ):
            raise ValueError("Invalid flow relation")
        option["series"] = [{"type": "sankey", "data": data["nodes"], "links": links}]
        option.pop("xAxis")
        option.pop("yAxis")
    return option


if __name__ == "__main__":
    cli(build)
