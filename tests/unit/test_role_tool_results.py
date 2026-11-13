"""Business tool errors retain actionable diagnostics at the model boundary."""

import json
from langchain_core.messages import ToolMessage


def test_validation_failure_preserves_errors_in_business_result():
    from research_copilot.subagents.middleware import normalize_business_message

    message = normalize_business_message(
        ToolMessage(
            tool_call_id="validate",
            content=json.dumps(
                {
                    "ok": False,
                    "errors": [
                        "Missing required section",
                        "Unauthorized evidence citation: E-999999",
                    ],
                }
            ),
        )
    )
    result = json.loads(message.content)
    assert result["error"]["code"] == "validation_failed"
    assert "Missing required section" in result["error"]["message"]
    assert "E-999999" in result["data"]["errors"][1]
