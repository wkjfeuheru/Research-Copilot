"""Offline reference renderers with identical frozen data for both engines."""

import json
from pathlib import Path
import subprocess
from echarts_options import build
from io_contract import cli


def render(
    *,
    kind,
    title,
    unit,
    data,
    output_dir,
    engine="matplotlib",
    metric_keys=None,
    evidence_ids=None,
    alt=None,
    width=1200,
    height=720,
):
    if (
        engine not in {"matplotlib", "echarts"}
        or not 320 <= width <= 2400
        or not 240 <= height <= 1600
    ):
        raise ValueError("Unsupported engine or canvas size")
    option = build(kind, title=title, unit=unit, data=data)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    (output / "option.json").write_text(
        json.dumps(option, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    if engine == "echarts":
        subprocess.run(
            [
                "node",
                "/opt/renderer/render_echarts.mjs",
                str(output / "option.json"),
                str(output / "chart.svg"),
                str(output / "chart.png"),
                str(width),
                str(height),
            ],
            check=True,
        )
    else:
        _matplotlib(kind, title, unit, data, output / "chart.png", width, height)
    manifest = {
        "schema_version": 1,
        "engine": engine,
        "kind": kind,
        "title": title,
        "theme_version": "research-v1",
        "width": width,
        "height": height,
        "data_file": "data.json",
        "static_file": "chart.png",
        "metric_keys": metric_keys or [],
        "evidence_ids": evidence_ids or [],
        "alt": alt or title,
    }
    if engine == "echarts":
        manifest.update(option_file="option.json", svg_file="chart.svg")
    (output / "chart.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    return manifest


def _matplotlib(kind, title, unit, data, path, width, height):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.patches import Rectangle, FancyArrowPatch
    import numpy as np

    # Register the locked font explicitly: Matplotlib otherwise picks the JP face of TTC.
    font_file = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    families = ["DejaVu Sans"]
    if font_file.is_file():
        font_manager.fontManager.addfont(str(font_file))
        families.insert(0, font_manager.FontProperties(fname=str(font_file)).get_name())
    plt.rcParams.update(
        {
            "font.family": families,
            "axes.unicode_minus": False,
        }
    )
    fig, ax = plt.subplots(
        figsize=(width / 100, height / 100), dpi=100, layout="constrained"
    )
    ax.set_title(title)
    ax.set_ylabel(unit)

    def nums(items):
        return [float(v) if v is not None else np.nan for v in items]

    try:
        if kind in {"bar", "line", "stacked_bar"}:
            x = np.arange(len(data["labels"]))
            bottom = np.zeros(len(x))
            count = len(data["series"])
            for i, s in enumerate(data["series"]):
                y = nums(s["values"])
                if kind == "line":
                    ax.plot(x, y, marker="o", label=s["name"])
                elif kind == "stacked_bar":
                    ax.bar(x, y, bottom=bottom, label=s["name"])
                    bottom += np.nan_to_num(y)
                else:
                    ax.bar(
                        x + (i - (count - 1) / 2) * 0.8 / count,
                        y,
                        width=0.8 / count,
                        label=s["name"],
                    )
            ax.set_xticks(x, data["labels"], rotation=20)
            ax.legend()
        elif kind == "scatter":
            a = np.array([[float(v) for v in p] for p in data["points"]])
            ax.scatter(a[:, 0], a[:, 1])
        elif kind == "waterfall":
            running = 0
            for i, v in enumerate(nums(data["values"])):
                ax.bar(
                    i,
                    abs(v),
                    bottom=min(running, running + v),
                    color="#0b766e" if v >= 0 else "#b54735",
                )
                running += v
            ax.set_xticks(range(len(data["labels"])), data["labels"])
        elif kind == "sensitivity_heatmap":
            grid = np.array([nums(row) for row in data["values"]])
            im = ax.imshow(grid, aspect="auto")
            fig.colorbar(im, ax=ax, label=unit)
            ax.set_xticks(range(len(data["x"])), data["x"])
            ax.set_yticks(range(len(data["y"])), data["y"])
            for i, row in enumerate(grid):
                for j, v in enumerate(row):
                    ax.text(j, i, f"{v:.4g}", ha="center", va="center")
        elif kind == "valuation_range":
            for i, (lo, hi) in enumerate(zip(nums(data["low"]), nums(data["high"]))):
                ax.plot([lo, hi], [i, i], linewidth=7)
            ax.set_yticks(range(len(data["labels"])), data["labels"])
            ax.set_xlabel(unit)
            ax.set_ylabel("")
        elif kind == "candlestick":
            fig.clear()
            ax, volume = fig.subplots(2, 1, sharex=True, height_ratios=[3, 1])
            ax.set_title(title)
            ax.set_ylabel(unit)
            volume.set_ylabel("Volume")
            for i, (o, c, l, h) in enumerate([nums(row) for row in data["ohlc"]]):
                color = "#0b766e" if c >= o else "#b54735"
                ax.vlines(i, l, h, color=color)
                ax.add_patch(
                    Rectangle(
                        (i - 0.25, min(o, c)), 0.5, max(abs(c - o), 0.0001), color=color
                    )
                )
            volume.bar(range(len(data["volume"])), nums(data["volume"]))
            volume.set_xticks(range(len(data["labels"])), data["labels"], rotation=20)
            ax.autoscale_view()
        elif kind == "treemap":
            total = sum(float(n["value"]) for n in data["nodes"])
            start = 0
            for i, node in enumerate(data["nodes"]):
                fraction = float(node["value"]) / total
                ax.add_patch(
                    Rectangle(
                        (start, 0),
                        fraction,
                        1,
                        facecolor=plt.get_cmap("Set2")(i % 8),
                        edgecolor="white",
                    )
                )
                ax.text(
                    start + fraction / 2,
                    0.5,
                    f"{node['name']}\n{node['value']} {unit}",
                    ha="center",
                )
                start += fraction
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.axis("off")
        else:
            # Reference flow diagram: each arrow is a real named relation, widths track flow.
            names = [n["name"] for n in data["nodes"]]
            positions = {n: (i % 2, i // 2) for i, n in enumerate(names)}
            maximum = max(float(link["value"]) for link in data["links"])
            for link in data["links"]:
                a, b = positions[link["source"]], positions[link["target"]]
                ax.add_patch(
                    FancyArrowPatch(
                        a,
                        b,
                        arrowstyle="->",
                        mutation_scale=18,
                        linewidth=1 + 8 * float(link["value"]) / maximum,
                        connectionstyle="arc3,rad=0.15",
                    )
                )
                ax.annotate(str(link["value"]), ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
            for name, point in positions.items():
                ax.text(*point, name, bbox={"facecolor": "white"})
            ax.set_xlim(-0.4, 1.4)
            ax.set_ylim(-0.5, max(p[1] for p in positions.values()) + 0.5)
            ax.axis("off")
        fig.savefig(path, dpi=100)
    finally:
        plt.close(fig)


if __name__ == "__main__":
    cli(render)
