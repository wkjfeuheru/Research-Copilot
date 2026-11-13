"""Shared read roots for static framework permissions and dispatch guards."""

from research_copilot.contracts import ROLE_NAMES


def role_read_roots(role: str) -> tuple[str, ...]:
    if role not in ROLE_NAMES:
        raise ValueError("Unknown role")
    skills = (
        ("/skills/writer/reports/references/",)
        if role == "reviewer"
        else (f"/skills/{role}/",)
    )
    return (
        "/workspace/inputs/",
        "/workspace/evidence/",
        "/workspace/artifacts/",
        "/instructions/",
        "/large_tool_results/",
        "/conversation_history/",
        *skills,
    )
