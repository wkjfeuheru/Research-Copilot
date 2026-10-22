"""使用真实 DeepSeek 模型运行一次 Planner（手动付费调用，不属于测试）。

用法（项目根目录）：
    # 先在 .env 设置 RC_RESEARCH_MODEL_API_KEY
    uv run python scripts/run_planner.py --question "分析贵州茅台最近三年的利润质量"

可选：
    --cutoff-date 2025-12-31
    --notes "重点关注经营现金流"

当前没有技能目录，脚本不接受技能参数；PlannerInput.selected_skill_ids 保持为空，
不加载或执行任何技能。
"""

import argparse
from datetime import date
import sys

from pydantic import ValidationError

from research_copilot.agents.planner import (
    Planner,
    PlannerError,
    PlannerInput,
)
from research_copilot.config import get_settings
from research_copilot.llm.deepseek import DeepSeekModelClient


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"日期必须是 YYYY-MM-DD：{value}"
        ) from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="使用 DeepSeek 生成并校验一份 Planner 输出。",
    )
    parser.add_argument("--question", required=True, help="研究问题")
    parser.add_argument(
        "--cutoff-date",
        type=_iso_date,
        default=None,
        help="研究截止日期，格式 YYYY-MM-DD",
    )
    parser.add_argument("--notes", default=None, help="人工补充说明")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    settings = get_settings()

    if not settings.research_model_configured:
        print(
            "未配置真实模型：请在 .env 设置 RC_RESEARCH_MODEL_API_KEY，"
            "并确认 RC_RESEARCH_MODEL 与 RC_RESEARCH_MODEL_BASE_URL。",
            file=sys.stderr,
        )
        return 2

    try:
        # 技能目录尚未实现，不暴露未接通的技能参数。
        request = PlannerInput(
            research_question=args.question,
            cutoff_date=args.cutoff_date,
            user_notes=args.notes,
        )
    except ValidationError as exc:
        print(f"Planner 输入不合法：{exc}", file=sys.stderr)
        return 2

    if settings.research_model == "deepseek-chat":
        print(
            "警告：官方文档显示 deepseek-chat 已于 2026-07-24 下线；"
            "如果当前网关不支持，请改用 deepseek-flash 或 deepseek-v4-pro。",
            file=sys.stderr,
        )

    print(
        f"调用 DeepSeek 模型 {settings.research_model}；"
        "这会发起真实 API 请求并产生费用。",
        file=sys.stderr,
    )

    try:
        client = DeepSeekModelClient.from_settings(settings)
        output = Planner(client).plan(request)
    except ValueError as exc:
        print(f"模型配置不可用：{exc}", file=sys.stderr)
        return 2
    except PlannerError as exc:
        print(f"规划失败 [{exc.code.value}]：{exc.message}", file=sys.stderr)
        return 1

    print(output.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
