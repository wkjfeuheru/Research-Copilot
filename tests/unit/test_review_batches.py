import hashlib
from types import SimpleNamespace
import pytest
from tests.unit.test_business import ControlRepository
from tests.unit.test_orchestration import context
from research_copilot.subagents.review_snapshot import SnapshotService


def service(texts):
    repo = ControlRepository()
    targets = {
        key: {
            "text": text,
            "hash": hashlib.sha256(text.encode()).hexdigest(),
            "tool": "read_snapshot_metric",
            "kind": "metric",
        }
        for key, text in texts.items()
    }
    content = {
        "targets": targets,
        "quality_targets": list(targets),
        "compliance_targets": [],
    }
    svc = SnapshotService(SimpleNamespace(repository=repo))

    async def load(ctx, snapshot):
        return content

    svc.load_snapshot = load
    return svc, repo, content


@pytest.mark.asyncio
async def test_paginated_complete_read_and_utf8_byte_limit():
    svc, repo, content = service({"metric:1": "数" * 20000, "metric:2": "second"})
    snapshot = {"snapshot_id": "s"}
    first = await svc.read_targets(
        context(), snapshot, "quality", ["metric:1", "metric:2"]
    )
    assert sum(len(i["content"].encode()) for i in first["items"]) <= 48 * 1024
    assert not first["items"][0]["complete"]
    assert not repo.state.role_data.get("review_reads", {}).get("s", {}).get("quality")
    second = await svc.read_targets(
        context(), snapshot, "quality", ["metric:1", "metric:2"], first["next_cursor"]
    )
    assert second["next_cursor"] is None
    assert (
        first["items"][0]["content"] + second["items"][0]["content"]
        == content["targets"]["metric:1"]["text"]
    )
    assert set(repo.state.role_data["review_reads"]["s"]["quality"]) == {
        "metric:1",
        "metric:2",
    }


@pytest.mark.asyncio
async def test_batch_prevalidates_every_target_and_cursor_cannot_skip_reads():
    svc, repo, content = service({"metric:1": "valid", "metric:2": "invalid"})
    content["targets"]["metric:2"]["hash"] = "0" * 64
    with pytest.raises(ValueError, match="hash"):
        await svc.read_targets(
            context(), {"snapshot_id": "s"}, "quality", ["metric:1", "metric:2"]
        )
    assert not repo.state.role_data.get("review_reads")
    with pytest.raises(PermissionError):
        await svc.read_targets(
            context(), {"snapshot_id": "s"}, "compliance", ["metric:1"]
        )
    with pytest.raises(ValueError, match="cursor"):
        await svc.read_targets(
            context(), {"snapshot_id": "s"}, "quality", ["metric:1"], "invented"
        )


@pytest.mark.asyncio
async def test_24_metrics_and_26_inputs_batch_read_equals_individual_targets():
    texts = {
        **{f"metric:{i}": f"metric value {i}" for i in range(24)},
        **{f"input:{i}": f"input source {i}" for i in range(26)},
    }
    svc, repo, _ = service(texts)
    received = {}
    keys = list(texts)
    for start in range(0, len(keys), 12):
        page = await svc.read_targets(
            context(), {"snapshot_id": "all"}, "quality", keys[start : start + 12]
        )
        assert page["next_cursor"] is None
        received.update({item["target_id"]: item["content"] for item in page["items"]})
    assert received == texts
    assert set(repo.state.role_data["review_reads"]["all"]["quality"]) == set(texts)


def test_inline_delivery_compacts_metric_receipts_and_respects_budget():
    import json
    from research_copilot.subagents.review_snapshot import SnapshotService

    service = SnapshotService(services=object())
    content = {
        "targets": {
            "document:0": {"kind": "document", "text": "alpha beta gamma"},
            "metric:0": {"kind": "metric", "text": "raw receipt json"},
        },
        "quality_targets": ["document:0", "metric:0"],
        "validation": {
            "metric_receipts": {
                "M-1": {
                    "metric_id": "M-1",
                    "key": "revenue_growth",
                    "value": "20.00",
                    "unit": "%",
                    "display_precision": 2,
                    "is_estimate": False,
                    "semantic_type": "growth",
                    "formula_id": "growth_percent",
                    "numerator_basis": "current",
                    "denominator_basis": "base",
                    "input_keys": ["rev_2025", "rev_2024"],
                    "evidence_ids": ["E-000001"],
                    "description": "Revenue growth",
                    "inputs": [{"evidence_id": "E-000001", "bloat": "x" * 4000}],
                }
            }
        },
    }
    delivered = service._inline_delivered(content, {"metric_ids": ["M-1"]}, "quality")
    assert delivered["document:0"] == "alpha beta gamma"
    row = json.loads(delivered["metric:0"])
    assert row["metric_id"] == "M-1" and row["value"] == "20.00"
    assert "inputs" not in row  # raw receipt lineage stays out of the inline view
    # Quote validation works against the delivered row, not the raw receipt.
    assert row["description"] in delivered["metric:0"]
    service.INLINE_MAX_BYTES = 10
    assert (
        service._inline_delivered(content, {"metric_ids": ["M-1"]}, "quality") is None
    )


def test_split_metric_review_targets_keep_their_own_frozen_text():
    from research_copilot.subagents.review_snapshot import SnapshotService

    targets = {
        "metric:0": {"kind": "metric", "text": "first half"},
        "metric:1": {"kind": "metric", "text": "second half"},
    }
    content = {
        "targets": targets,
        "quality_targets": list(targets),
        "validation": {"metric_receipts": {"M-1": {}}},
    }
    assert SnapshotService(object())._inline_delivered(
        content, {"metric_ids": ["M-1"]}, "quality"
    ) == {k: v["text"] for k, v in targets.items()}
