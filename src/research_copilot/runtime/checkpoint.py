"""Exact application types needed by existing frozen checkpoints."""

import importlib
import sys

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def checkpoint_serializer():
    allowed = [
        ("research_copilot.subagents.reviewer", "BranchReview"),
        ("research_copilot.subagents.reviewer", "ReviewIssue"),
        ("research_copilot.subagents.reviewer", "IssueClosure"),
        ("research_copilot.subagents.contracts", "SearchResult"),
        ("research_copilot.subagents.contracts", "EvidenceFact"),
        ("research_copilot.subagents.contracts", "AnalysisResult"),
        ("research_copilot.subagents.contracts", "WritingResult"),
        ("research_copilot.subagents.contracts", "Gap"),
        ("research_copilot.contracts", "ArtifactRef"),
        ("research_copilot.contracts", "RoleResult"),
        ("research_copilot.contracts", "ReviewResult"),
    ]
    # Old bytes name these exact modules. Alias only the known model modules in
    # the import cache; new classes/writes retain their canonical package name.
    # Keep the framework's exact type allowlist and pickle protection intact.
    legacy = []
    for module_name in dict.fromkeys(module for module, _ in allowed):
        module = importlib.import_module(module_name)
        old_name = module_name.replace("research_copilot.", "research_copilot_v2.", 1)
        previous = sys.modules.setdefault(old_name, module)
        if previous is not module:
            raise ValueError("Conflicting legacy checkpoint model module")
        legacy.extend(
            (old_name, name) for current, name in allowed if current == module_name
        )
    return JsonPlusSerializer(
        pickle_fallback=False,
        allowed_msgpack_modules=[*allowed, *legacy],
    )
