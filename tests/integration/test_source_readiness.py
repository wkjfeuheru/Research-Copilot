"""A source outage must be visible and must not create a paid research run."""

import pytest

from research_copilot.sources.financekb import SourceStatus
from tests.integration.test_durable_web_runs import backend, client
from tests.integration import test_retrospective_leases as lease_fixtures

lease_stores = lease_fixtures.lease_stores


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ((SourceStatus("fundamentals", False, "contract_mismatch", ()),), 503),
        ((), 503),
        (
            (
                SourceStatus("fundamentals", False, "timeout", ()),
                SourceStatus("knowledge", True, None, ("financekb_search_research",)),
            ),
            200,
        ),
    ],
)
async def test_readiness_tracks_real_source_discovery(
    lease_stores, tmp_path, statuses, expected
):
    current = backend(lease_stores[0], tmp_path)
    current.source_statuses = statuses
    async with client(current) as http:
        ready = await http.get("/api/ready")
        assert ready.status_code == expected
        assert ready.json()["checks"]["sources"]["status"] == (
            "ok" if expected == 200 else "unavailable"
        )
        assert ready.json()["sources"]["available_domains"] == sum(
            s.available for s in statuses
        )
        assert (await http.get("/api/live")).status_code == 200


async def test_total_source_outage_blocks_admission_before_persisting_or_calling_model(
    lease_stores, tmp_path
):
    current = backend(lease_stores[0], tmp_path)
    current.source_statuses = (
        SourceStatus("fundamentals", False, "contract_mismatch", ()),
    )
    try:
        async with client(current) as http:
            response = await http.post(
                "/api/reports", json={"user_request": "宁德时代毛利率环比归因"}
            )
            assert response.status_code == 503
            assert response.json()["code"] == "sources_unavailable"
            assert "数据源" in response.json()["detail"]
            assert (await http.get("/api/reports")).json()["reports"] == []
        assert not current.session.entered.is_set()
    finally:
        await current.runs.aclose()


async def test_direct_session_start_cannot_bypass_source_admission(lease_stores):
    from research_copilot.runtime.session import SessionRecord, SessionService
    from research_copilot.runtime.budget import TokenBudget

    store = lease_stores[0]
    record = SessionRecord("source-outage", "local", "v2", "policy", "source-outage")
    await store.create(
        record, TokenBudget(limit=10000, budget_id="source-outage").snapshot()
    )

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "No graph or model should be constructed during a source outage"
        )

    session = SessionService(
        store=store, agent_factory=forbidden, policy_version="policy"
    )
    session.source_statuses = (
        SourceStatus("fundamentals", False, "contract_mismatch", ()),
    )
    with pytest.raises(RuntimeError, match="数据源"):
        await session.start("source-outage", "local", "宁德时代毛利率")
    assert (await store.load("source-outage", "local")).run_id == ""
