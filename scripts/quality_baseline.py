"""Run: uv run python -m scripts.quality_baseline --output output/baseline.json"""

import argparse
import asyncio
import json
from pathlib import Path

from research_copilot.runtime.quality_baseline import evaluate_suite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/engineering_baseline"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    result = asyncio.run(evaluate_suite(args.fixtures))
    if args.compare:
        previous = json.loads(args.compare.read_text("utf-8"))
        if previous["fixture_sha256"] != result["fixture_sha256"]:
            raise SystemExit("Comparison requires the same frozen case set")
        result["regression"] = result["contract_accuracy"] < previous["contract_accuracy"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{result['case_count']} frozen cases; contract accuracy={result['contract_accuracy']}; synthetic usage only")
    if result["contract_accuracy"] < 1 or result.get("regression"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
