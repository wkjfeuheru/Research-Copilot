"""Registered role scaffolds; business implementations are added independently."""

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from research_copilot.contracts import (
    AUTONOMOUS_ROLES,
    Role,
    RoleResult,
    RunContext,
    UnavailableReviewResult,
)


def unavailable_result(role: Role) -> RoleResult:
    payload = {
        "role": role,
        "status": "unavailable",
        "summary": f"{role} 已注册，具体业务逻辑尚未实现。",
        "references": [],
        "gaps": [f"待接入 {role} 的业务实现。"],
    }
    contract = UnavailableReviewResult if role == "reviewer" else RoleResult
    return contract.model_validate(payload)


class PlaceholderRoleMiddleware(AgentMiddleware):
    """Keep declarative SubAgent registration, exit before model/tool execution."""

    def __init__(self, role: str):
        if role not in AUTONOMOUS_ROLES:
            raise ValueError("Only autonomous roles use placeholder middleware")
        self.role = role

    # Node hooks may jump to end in the pinned LangChain version.
    # https://docs.langchain.com/oss/python/langchain/middleware/custom
    @hook_config(can_jump_to=["end"])
    async def abefore_agent(self, state, runtime):
        return {
            "messages": [
                AIMessage(content=unavailable_result(self.role).model_dump_json())
            ],
            "jump_to": "end",
        }


def build_placeholder_review_graph():
    """Reviewer CompiledSubAgent scaffold: no assessment, approval or interrupt."""

    async def unavailable(state):
        # Replace this node with the review workflow when that business work is implemented.
        return {
            "messages": [
                AIMessage(content=unavailable_result("reviewer").model_dump_json())
            ]
        }

    graph = StateGraph(MessagesState, context_schema=RunContext)
    graph.add_node("unavailable", unavailable)
    graph.add_edge(START, "unavailable")
    graph.add_edge("unavailable", END)
    return graph.compile()
