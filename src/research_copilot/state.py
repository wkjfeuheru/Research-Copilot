"""研究图的共享状态（blackboard）。

本阶段只定义已实现节点真正使用的通道：Planner 的输入请求和已校验计划。
后续 Data Fetcher、Analyst、Risk Checker、Writer 实现时再增加各自负责的字段，
避免提前堆积空壳字段。

状态中的值使用 JSON 可序列化的字典，而不是直接存放 Pydantic 实例；节点在边界
显式 ``model_validate`` 契约，便于后续接入 checkpoint 和持久化。
"""

from typing import Any, TypedDict


class ResearchState(TypedDict, total=False):
    """研究图的共享状态；每个字段由明确的责任节点写入。"""

    request: dict[str, Any]
    """PlannerInput 的 JSON 形式输入。"""

    plan: dict[str, Any]
    """PlannerOutput 通过 Schema 与业务校验后的 JSON 形式结果。"""


__all__ = ["ResearchState"]
