"""Reports freeze the approved source contract, not transient domain health."""

from types import SimpleNamespace

from research_copilot.runtime.source_contract import source_contract_compatible

CURRENT = {"sha256": "a" * 64, "normalization_version": 2}


def state(manifest=...):
    role_data = {} if manifest is ... else {"runtime_manifest": manifest}
    return SimpleNamespace(role_data=role_data)


def test_new_report_has_no_frozen_contract_yet():
    assert source_contract_compatible(state(), CURRENT)
    assert source_contract_compatible(state({"source_contract": {}}), {})


def test_matching_contract_remains_writable_across_domain_health_changes():
    manifest = {
        "source_contract": dict(CURRENT),
        "sources": {"financekb_get_ohlcv": {"schema_hash": "b" * 64}},
    }
    assert source_contract_compatible(state(manifest), CURRENT)


def test_missing_or_changed_frozen_contract_blocks_writes():
    old = {"sha256": "c" * 64, "normalization_version": 1}
    assert not source_contract_compatible(
        state({"source_contract": old}), CURRENT
    )
    assert not source_contract_compatible(
        state({"sources": {"tavily": "old"}}), CURRENT
    )
