from research_copilot.sandbox.execution_policy import execution_window_start


def test_only_matching_host_extension_can_extend_window():
    records = [{"execution_id": "e", "started_at": 10}]
    grant = {
        "plan": "p",
        "rework": None,
        "actor_id": "user",
        "reason": "retry",
        "prior_execution_ids": ["e"],
        "additional_attempts": 3,
        "authorized_at": "1970-01-01T00:01:40+00:00",
    }
    assert execution_window_start(records, ("p", None), [grant], now=200) == 100
    assert execution_window_start(records, ("other", None), [grant], now=200) == 10
    assert (
        execution_window_start(
            records,
            ("p", None),
            [dict(grant, prior_execution_ids=["missing"])],
            now=200,
        )
        == 10
    )
    assert execution_window_start(records, ("p", None), [grant], now=50) == 10
