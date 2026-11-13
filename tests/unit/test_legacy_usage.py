"""Offline usage aggregation must not count tool caches or resumed sessions twice."""

import json
from decimal import Decimal

import pytest


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def usage(prompt=100, completion=20):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def test_progress_counts_model_events_and_groups_by_routed_model(tmp_path):
    from scripts.report_usage import build_report, parse_prices

    path = tmp_path / "q1/progress.jsonl"
    path.parent.mkdir()
    events = [
        {"event": "model_start", "number": 1, "stage": "plan"},
        {
            "event": "model_end",
            "number": 1,
            "stage": "plan",
            "routed_model": "deepseek-flash",
            "usage": usage(),
        },
        {
            "event": "model_end",
            "number": 2,
            "stage": "write",
            "routed_model": "deepseek-chat",
            "usage": usage(200, 40),
        },
    ]
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    before = path.read_bytes()
    report = build_report(progress=tmp_path, prices=parse_prices(["2,4"]))
    assert report["totals"]["calls"] == 2
    assert report["totals"]["total_tokens"] == 360
    assert Decimal(report["totals"]["estimated_cost"]) == Decimal("0.00084")
    assert {(r["node"], r["model"]) for r in report["rows"]} == {
        ("plan", "deepseek-flash"),
        ("write", "deepseek-chat"),
    }
    assert path.read_bytes() == before


def test_receipts_include_all_locations_without_double_counting_complete_sessions(
    tmp_path,
):
    from scripts.report_usage import build_report

    root = tmp_path / "reports/r1"
    write_json(
        root / "collection/run/results/task.json",
        {"key": "task:x", "result": {"result": {"task_id": "DC-1"}, "usage": usage()}},
    )
    write_json(
        root / "collection/run/results/tool.json",
        {"result": {"ok": True, "data": {"anything": 1}}},
    )
    write_json(
        root / "collection_sessions/complete.json",
        {"payload": {"state": "complete", "usage": usage()}},
    )
    write_json(
        root / "collection_sessions/pending.json",
        {"payload": {"state": "pending", "usage": usage(50, 10)}},
    )
    for path in [
        "analysis/r0001/tasks/AN-1.json",
        "writing/r0001/outline.json",
        "writing/r0001/sections/S-01.json",
        "review/r0001/qc.json",
        "review/r0001/compliance.json",
    ]:
        write_json(
            root / path,
            {"usage": usage(), "model": "historical-model"}
            if "/qc" in path
            else {"usage": usage()},
        )
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    report = build_report(reports=root.parent)
    assert report["totals"]["receipt_count"] == 7
    assert report["totals"]["total_tokens"] == 780
    assert report["totals"]["calls"] is None
    assert any(
        row["model"] == "historical-model" and row["model_source"] == "receipt"
        for row in report["rows"]
    )
    assert any(
        row["model_source"] == "current_config_estimate" for row in report["rows"]
    )
    assert report["warnings"]
    assert before == {p: p.read_bytes() for p in root.rglob("*.json")}


def test_bad_usage_and_json_are_reported_without_fabricating_zero_cost(tmp_path):
    from scripts.report_usage import build_report

    path = tmp_path / "q1/progress.jsonl"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"event": "model_end", "stage": "collect", "number": 1})
        + "\n{broken\n",
        encoding="utf-8",
    )
    report = build_report(progress=tmp_path)
    assert report["totals"]["calls"] == 1
    assert report["totals"]["missing_usage"] == 1
    assert report["totals"]["estimated_cost"] is None
    assert report["errors"]


@pytest.mark.parametrize(
    "value", ["-1,2", "NaN,2", "2,Infinity", "bad", "model=2", "1,2,3"]
)
def test_invalid_prices_are_rejected(value):
    from scripts.report_usage import parse_prices

    with pytest.raises(ValueError):
        parse_prices([value])


def test_model_specific_price_leaves_unpriced_models_explicit(tmp_path):
    from scripts.report_usage import build_report, parse_prices

    path = tmp_path / "progress.jsonl"
    path.write_text(
        json.dumps(
            {
                "event": "model_end",
                "number": 1,
                "stage": "collect",
                "routed_model": "other",
                "usage": usage(),
            }
        ),
        encoding="utf-8",
    )
    report = build_report(
        progress=tmp_path, prices=parse_prices(["deepseek-flash=2,4"])
    )
    assert report["totals"]["estimated_cost"] is None
    assert report["warnings"]


def test_duplicate_progress_event_is_not_billed_twice(tmp_path):
    from scripts.report_usage import build_report

    event = {
        "event": "model_end",
        "number": 1,
        "stage": "write",
        "routed_model": "deepseek-chat",
        "usage": usage(),
    }
    (tmp_path / "progress.jsonl").write_text(
        "\n".join([json.dumps(event)] * 2), encoding="utf-8"
    )
    report = build_report(progress=tmp_path)
    assert report["totals"]["calls"] == 1
    assert report["totals"]["total_tokens"] == 120


def test_usage_cli_json_and_invalid_path_exit_codes(tmp_path, capsys):
    from scripts.report_usage import main

    assert main(["--progress", str(tmp_path), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["errors"]
    path = tmp_path / "progress.jsonl"
    path.write_text(
        json.dumps(
            {
                "event": "model_end",
                "number": 1,
                "stage": "qc",
                "routed_model": "deepseek-chat",
                "usage": usage(),
            }
        ),
        encoding="utf-8",
    )
    assert main(["--progress", str(path), "--json", "--price", "1,2"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["calls"] == 1
