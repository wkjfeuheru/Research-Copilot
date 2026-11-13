"""Read-only receipt/progress usage report; prices are user-supplied per million tokens."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from research_copilot.llm.config import load_models_config  # noqa: E402

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def parse_prices(values: list[str]) -> dict[str, tuple[Decimal, Decimal]]:
    """Accept INPUT,OUTPUT or MODEL=INPUT,OUTPUT; amounts share the caller's currency."""
    prices = {}
    for value in values:
        model, separator, rates = value.partition("=")
        if not separator:
            model, rates = "*", value
        parts = rates.split(",")
        if not model.strip() or len(parts) != 2:
            raise ValueError("price 必须为 INPUT,OUTPUT 或 MODEL=INPUT,OUTPUT（每百万 tokens）")
        try:
            pair = tuple(Decimal(part.strip()) for part in parts)
        except InvalidOperation as exc:
            raise ValueError("price 必须为有限的非负数字") from exc
        if any(not rate.is_finite() or rate < 0 for rate in pair):
            raise ValueError("price 必须为有限的非负数字")
        prices[model.strip()] = pair
    return prices


def build_report(
    *, reports: Path | None = None, progress: Path | None = None,
    models_path: Path = ROOT / "config/models.example.toml",
    prices: dict[str, tuple[Decimal, Decimal]] | None = None,
) -> dict[str, Any]:
    """Aggregate recorded usage without database/model access or artifact writes.

    Receipt count is an Agent/artifact count, not an exact model-request count.
    Completed collection sessions are excluded: their cumulative usage is in task receipts.
    """
    warnings: list[str] = []
    errors: list[str] = []
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    prices = prices or {}
    models = {}
    if progress is None:
        try:
            bindings = load_models_config(models_path).roles
            role_for_node = {'plan':'supervisor','collect':'searcher','analyze':'analysist','write':'writer','qc':'reviewer','compliance':'reviewer','diagnose':'supervisor'}
            models = {node: bindings[role].model for node, role in role_for_node.items()}
        except (OSError, ValueError) as exc:
            warnings.append(f"当前模型配置不可用（{type(exc).__name__}）；历史模型标为 unknown")
        warnings.append("历史 receipt 反映当时配置；缺少模型名时仅按当前配置估计，不代表历史实际模型。")
        warnings.append("receipt 未完整记录模型请求次数：calls=null，receipt_count 是产物/Agent 次数；精确调用数请用 --progress。")
        warnings.append("receipt 统计不覆盖未落盘的请求以及 plan/diagnose 用量；失败/续跑可能仅保留部分用量。")

    def read(path: Path):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON root must be an object")
            return value
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{path}: {type(exc).__name__}")
            return None

    def add(report_id: str, node: str, payload: dict, *, event: bool = False):
        model = payload.get("routed_model") or payload.get("model")
        source = "progress" if event else "receipt"
        if not isinstance(model, str) or not model:
            model = models.get(node, "unknown")
            source = "unknown" if event else "current_config_estimate"
        key = (report_id, node, model, source)
        row = groups.setdefault(key, {
            "report_id": report_id, "node": node, "model": model, "model_source": source,
            "calls": 0 if event else None, "receipt_count": 0, "missing_usage": 0,
            **dict.fromkeys(TOKEN_FIELDS, 0),
        })
        if event:
            row["calls"] += 1
        else:
            row["receipt_count"] += 1
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            row["missing_usage"] += 1
            return
        values = [usage.get(field) for field in TOKEN_FIELDS]
        if any(type(value) is not int or value < 0 for value in values):
            errors.append(f"{report_id}/{node}: usage 必须包含非负整数 prompt/completion/total tokens")
            row["missing_usage"] += 1
            return
        for field, value in zip(TOKEN_FIELDS, values):
            row[field] += value

    if progress is not None:
        root = Path(progress)
        paths = [root] if root.is_file() else sorted(root.rglob("progress.jsonl"))
        if not paths:
            errors.append(f"未找到 progress.jsonl：{root}")
        for path in paths:
            seen: dict[int, dict] = {}
            try:
                with path.open(encoding="utf-8") as handle:
                    for number, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                raise ValueError("event must be an object")
                        except ValueError:
                            errors.append(f"{path}:{number}: 非法 JSON event")
                            continue
                        if event.get("event") != "model_end":
                            continue
                        identifier = event.get("number")
                        if type(identifier) is int:
                            if identifier in seen:
                                if seen[identifier] != event:
                                    errors.append(f"{path}:{number}: 冲突的 model_end number={identifier}")
                                continue
                            seen[identifier] = event
                        add(str(event.get("report_id") or path.parent.name), str(event.get("stage") or "unknown"), event, event=True)
            except (OSError, UnicodeError) as exc:
                errors.append(f"{path}: {type(exc).__name__}")
    else:
        root = Path(reports) if reports is not None else ROOT / "data/reports"
        if not root.is_dir():
            errors.append(f"报告工作区不存在：{root}")
            roots = []
        elif any((root / part).is_dir() for part in ("collection", "collection_sessions", "analysis", "writing", "review")):
            roots = [root]
        else:
            roots = sorted(p for p in root.iterdir() if p.is_dir())
        patterns = (
            ("collect", "collection/*/results/*.json"),
            ("collect", "collection_sessions/*.json"),
            ("analyze", "analysis/*/tasks/AN-*.json"),
            ("write", "writing/*/outline.json"),
            ("write", "writing/*/sections/S-*.json"),
            ("qc", "review/*/qc.json"),
            ("compliance", "review/*/compliance.json"),
        )
        for workspace in roots:
            for node, pattern in patterns:
                for path in sorted(workspace.glob(pattern)):
                    payload = read(path)
                    if payload is None:
                        continue
                    if pattern.startswith("collection/"):
                        payload = payload.get("result")
                        if not isinstance(payload, dict) or "usage" not in payload:
                            continue  # Tool response cache, not a DC Agent outcome.
                    elif pattern.startswith("collection_sessions/"):
                        payload = payload.get("payload", payload)
                        if not isinstance(payload, dict):
                            errors.append(f"{path}: 非法会话 payload")
                            continue
                        if payload.get("state") == "complete":
                            warnings.append(f"{path}: complete 会话不重复计入，最终用量以 DC receipt 为准")
                            continue
                    add(workspace.name, node, payload)

    rows = [groups[key] for key in sorted(groups)]
    for row in rows:
        rate = prices.get(row["model"], prices.get("*"))
        row["estimated_cost"] = None
        if rate is not None and not row["missing_usage"] and row["model"] != "unknown":
            row["estimated_cost"] = str((row["prompt_tokens"] * rate[0] + row["completion_tokens"] * rate[1]) / Decimal(1_000_000))
        elif prices and rate is None:
            warnings.append(f"{row['model']}: 未提供单价，金额未知")
    totals = {
        **{field: sum(row[field] for row in rows) for field in (*TOKEN_FIELDS, "receipt_count", "missing_usage")},
        "calls": sum(row["calls"] for row in rows) if progress is not None else None,
        "estimated_cost": str(sum((Decimal(row["estimated_cost"]) for row in rows), Decimal(0))) if rows and all(row["estimated_cost"] is not None for row in rows) else None,
    }
    if totals["missing_usage"]:
        warnings.append("存在未记录 usage 的请求；tokens 仅为已知小计，金额不完整。")
    return {"source": "progress" if progress is not None else "receipts", "rows": rows, "totals": totals, "warnings": warnings, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--reports", type=Path, help="报告工作区父目录或单个报告目录")
    source.add_argument("--progress", type=Path, help="progress.jsonl 文件或包含该文件的验收目录")
    parser.add_argument("--models", type=Path, default=ROOT / "config/models.example.toml")
    parser.add_argument("--price", action="append", default=[], help="每百万 tokens 单价 INPUT,OUTPUT 或 MODEL=INPUT,OUTPUT，可重复")
    parser.add_argument("--json", action="store_true", help="将结构化报表写到标准输出")
    args = parser.parse_args(argv)
    try:
        prices = parse_prices(args.price)
    except ValueError as exc:
        parser.error(str(exc))
    report = build_report(reports=args.reports, progress=args.progress, models_path=args.models, prices=prices)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("report\tnode\tmodel\tmodel_source\tcalls\treceipts\tprompt\tcompletion\ttotal\tcost")
        for row in [*report["rows"], {"report_id": "TOTAL", "node": "-", "model": "-", "model_source": "-", **report["totals"]}]:
            print("\t".join(str(row.get(key)) if row.get(key) is not None else "unknown" for key in ("report_id", "node", "model", "model_source", "calls", "receipt_count", *TOKEN_FIELDS, "estimated_cost")))
        for note in report["warnings"]:
            print("WARNING: " + note, file=sys.stderr)
        for error in report["errors"]:
            print("ERROR: " + error, file=sys.stderr)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
