import importlib


def test_legacy_skill_read_resolves_to_canonical_skill():
    from research_copilot.workspace import bundled_resources
    from research_copilot.workspace.filesystem import ResourceFilesystemBackend

    root = bundled_resources()
    backend = ResourceFilesystemBackend(root_dir=root / "skills", read_only=True)
    old = backend._resolve_path("/analysist/financial_modeling/SKILL.md")
    current = backend._resolve_path("/analysist/financial-modeling/SKILL.md")
    assert old == current
    assert current.is_file()


def test_checkpoint_exact_allowlist_roundtrips_nested_review():
    from research_copilot.subagents.reviewer import BranchReview, ReviewIssue

    serde = importlib.import_module(
        "research_copilot.runtime.checkpoint"
    ).checkpoint_serializer()
    value = BranchReview(
        pass_confidence=0.8,
        checks_complete=True,
        covered_targets=["document:0"],
        issues=[
            ReviewIssue(
                category="quality",
                severity="major",
                target_id="document:0",
                location="body",
                quote="text",
                basis="facts",
                reason="unsupported",
                suggestion="revise",
            )
        ],
    )
    assert serde.loads_typed(serde.dumps_typed(value)) == value
