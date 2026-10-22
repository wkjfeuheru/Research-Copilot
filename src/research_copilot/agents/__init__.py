"""研究 Agent 包（阶段一仅建立边界）。

后续职责：
- Planner：理解研究问题、截止日期与范围，形成计划及数据需求。
- Data Fetcher：通过受控工具获取、筛选资料，标注证据缺口并提炼可追溯记忆。
- Analyst：组织分析，调用可信计算与制图能力，关联证据。
- Risk Checker：检查证据支撑、风险与缺口，给出通过、修订或人工建议。
- Writer：把已审核分析组织为带引用的 Markdown 简报。
- 交付检查：确定性检查引用、图表和结论一致性。

编排阶段再添加 ``state.py``、``orchestrator.py`` 和节点输入输出契约。本阶段
不放置空实现、RAG、隐式 mock 或 native fallback。
"""

__all__: list[str] = []
