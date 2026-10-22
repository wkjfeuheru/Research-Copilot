"""导出 Planner 输入/输出 JSON Schema。

用法（项目根目录）：
    uv run python scripts/export_planner_schemas.py
"""

from research_copilot.agents.planner.contracts import export_planner_schemas


def main() -> None:
    for path in export_planner_schemas():
        print(path)


if __name__ == "__main__":
    main()
