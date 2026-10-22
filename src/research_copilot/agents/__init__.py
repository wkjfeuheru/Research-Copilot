"""研究 Agent 包。

已实现：
- Planner：理解研究问题、范围与截止日期，形成计划、数据需求和研究步骤；
  见 ``research_copilot.agents.planner``。

后续职责：
- Data Fetcher：通过受控工具获取、筛选资料，标注证据缺口并提炼可追溯记忆。
- Analyst：组织分析，调用可信计算与制图能力，关联证据。
- Risk Checker：检查证据支撑、风险与缺口，给出通过、修订或人工建议。
- Writer：把已审核分析组织为带引用的 Markdown 简报。
- 交付检查：确定性检查引用、图表和结论一致性。

共享图状态位于 ``research_copilot.state``，当前只包含 Planner 已使用的通道；
后续节点实现时再增加各自字段。本模块不放置空实现、RAG、隐式 mock 或 native fallback。
"""

__all__: list[str] = []
