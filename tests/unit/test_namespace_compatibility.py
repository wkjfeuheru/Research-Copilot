"""Package renaming must preserve frozen objects and deserialization boundaries."""

import base64
import json
from pathlib import Path

import pytest
from pydantic import BaseModel


def test_checkpoint_written_before_package_rename_restores_exact_model_types():
    from research_copilot.contracts import ArtifactRef
    from research_copilot.runtime.checkpoint import checkpoint_serializer
    from research_copilot.subagents.reviewer import BranchReview

    path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/legacy_checkpoints/review_namespace.json"
    )
    fixture = json.loads(path.read_text(encoding="utf-8"))
    serde = checkpoint_serializer()
    restored = serde.loads_typed(
        (fixture["format"], base64.b64decode(fixture["payload_base64"]))
    )
    assert isinstance(restored["review"], BranchReview)
    assert restored["review"].model_dump(mode="json") == fixture["review"]
    assert isinstance(restored["artifact"], ArtifactRef)
    assert restored["artifact"].model_dump() == fixture["artifact"]
    assert b"research_copilot_v2" not in serde.dumps_typed(restored)[1]


@pytest.mark.parametrize(
    "module_name",
    ["research_copilot.subagents.reviewer", "research_copilot_v2.subagents.reviewer"],
)
def test_namespace_compatibility_does_not_allow_unregistered_types(
    module_name, monkeypatch
):
    from research_copilot.runtime.checkpoint import checkpoint_serializer
    from research_copilot.subagents import reviewer

    class Unexpected(BaseModel):
        payload: str

    Unexpected.__module__ = module_name
    monkeypatch.setattr(reviewer, "Unexpected", Unexpected, raising=False)
    serde = checkpoint_serializer()
    encoded = serde.dumps_typed(Unexpected.model_construct(payload="untrusted"))
    assert not isinstance(serde.loads_typed(encoded), Unexpected)


def test_storage_and_replay_identities_match_frozen_values():
    from research_copilot.runtime.identity import (
        DEFAULT_POLICY_VERSION,
        PROJECTION_FORMAT,
        RUNTIME_VERSION,
        advisory_lock_identity,
        checkpoint_thread_id,
        confirmation_receipt_id,
    )
    from research_copilot.runtime.storage import sessions
    from research_copilot.runtime.workflow import dispatch_id

    assert sessions.name == "sessions"
    assert RUNTIME_VERSION == "v2"
    assert DEFAULT_POLICY_VERSION == "v2-policy-1"
    assert PROJECTION_FORMAT == "v2-json-segments-1"
    assert checkpoint_thread_id("historical-report") == "v2:historical-report"
    assert (
        dispatch_id("historical-report", "historical-call")
        == "021aa4bd0bb95eefaea48aec1d8be953"
    )
    assert advisory_lock_identity("historical-report") == 8021103507496512526
    assert confirmation_receipt_id("approved-hash") == "v2:approved-hash"


def test_namespace_rename_preserves_historical_resource_fingerprint():
    import hashlib

    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures/legacy_checkpoints/review_namespace.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    # Current skills may evolve; a historical report retains its own snapshot.
    manifest = fixture_path.with_name("resources_manifest.json").read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == fixture["resources_manifest_sha256"]
