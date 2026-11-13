"""Reviewer StateGraph with explicit parallel barrier and restart-safe interrupt."""

from typing import NotRequired
from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from research_copilot.contracts import RunContext


class ReviewState(MessagesState):
    snapshot: NotRequired[dict]
    precheck: NotRequired[dict]
    quality: NotRequired[dict]
    compliance: NotRequired[dict]
    assessment: NotRequired[dict]
    human_submission: NotRequired[dict]
    feedback_errors: NotRequired[list[str]]
    feedback_route: NotRequired[str]
    decision: NotRequired[dict]
    result: NotRequired[dict]


def build_reviewer_graph(service):
    async def prepare(state, runtime: Runtime[RunContext]):
        return {
            "snapshot": await service.prepare_snapshot(runtime.context),
            "human_submission": None,
            "feedback_errors": [],
            "decision": None,
            "result": None,
        }

    async def precheck(state, runtime: Runtime[RunContext]):
        return {"precheck": await service.precheck(runtime.context, state["snapshot"])}

    async def quality(state, runtime: Runtime[RunContext]):
        return {
            "quality": await service.review_branch(
                runtime.context, state["snapshot"], state["precheck"], "quality"
            )
        }

    async def compliance(state, runtime: Runtime[RunContext]):
        return {
            "compliance": await service.review_branch(
                runtime.context, state["snapshot"], state["precheck"], "compliance"
            )
        }

    async def aggregate(state, runtime: Runtime[RunContext]):
        return {
            "assessment": await service.aggregate(
                runtime.context,
                state["snapshot"],
                state["precheck"],
                state["quality"],
                state["compliance"],
            )
        }

    def auto_pass(state):
        return {
            "decision": {
                "outcome": "passed",
                "source": "automatic",
                "summary": "Frozen version meets complete review policy",
            }
        }

    def human_review(state):
        submission = interrupt(
            {
                "kind": "reviewer_deep_review",
                "review_session_id": state["snapshot"]["review_session_id"],
                "snapshot": state["snapshot"],
                "assessment": state["assessment"],
                "errors": state.get("feedback_errors", []),
                "allowed_decisions": ["approve", "request_changes", "reject"],
            }
        )
        return {"human_submission": submission}

    async def feedback(state, runtime: Runtime[RunContext]):
        result = await service.validate_feedback(
            runtime.context,
            state["snapshot"],
            state["assessment"],
            state["human_submission"],
        )
        return {
            "feedback_route": result["route"],
            "feedback_errors": result["errors"],
            "decision": result["decision"],
        }

    def stale(state):
        return {
            "decision": {
                "outcome": "stale",
                "source": "system",
                "summary": "Draft version changed; dispatch a new review",
            }
        }

    async def finalize(state, runtime: Runtime[RunContext]):
        output = await service.commit_review(
            runtime.context, state["snapshot"], state["assessment"], state["decision"]
        )
        return {
            "result": output.model_dump(),
            "messages": [AIMessage(content=output.model_dump_json())],
        }

    graph = StateGraph(ReviewState, context_schema=RunContext)
    nodes = {
        "prepare_snapshot": prepare,
        "deterministic_checks": precheck,
        "quality_review": quality,
        "compliance_review": compliance,
        "aggregate_risk": aggregate,
        "auto_pass": auto_pass,
        "human_review": human_review,
        "process_feedback": feedback,
        "stale_result": stale,
        "finalize": finalize,
    }
    for name, node in nodes.items():
        graph.add_node(name, node)
    graph.add_edge(START, "prepare_snapshot")
    graph.add_edge("prepare_snapshot", "deterministic_checks")
    graph.add_edge("deterministic_checks", "quality_review")
    graph.add_edge("deterministic_checks", "compliance_review")
    # https://docs.langchain.com/oss/python/langgraph/graph-api
    graph.add_edge(["quality_review", "compliance_review"], "aggregate_risk")
    graph.add_conditional_edges(
        "aggregate_risk",
        lambda s: (
            "auto_pass" if s["assessment"]["auto_pass_eligible"] else "human_review"
        ),
        {"auto_pass": "auto_pass", "human_review": "human_review"},
    )
    graph.add_edge("auto_pass", "finalize")
    graph.add_edge("human_review", "process_feedback")
    graph.add_conditional_edges(
        "process_feedback",
        lambda s: s["feedback_route"],
        {"retry": "human_review", "stale": "stale_result", "finalize": "finalize"},
    )
    graph.add_edge("stale_result", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()
