from research_copilot.web.workspace import normalize_interrupts


def test_review_projection_preserves_issue_identity_for_human_dispositions():
    view = normalize_interrupts(
        [
            {
                "id": "i",
                "value": {
                    "kind": "reviewer_deep_review",
                    "review_session_id": "r",
                    "snapshot": {"snapshot_id": "s"},
                    "assessment": {
                        "checks_complete": True,
                        "issues": [
                            {"issue_id": "issue-1", "severity": "major", "hard": False}
                        ],
                    },
                },
            }
        ]
    )[0]
    assert view["review"]["issues"][0]["issue_id"] == "issue-1"
