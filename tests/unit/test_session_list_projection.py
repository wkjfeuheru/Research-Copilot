"""Listing sessions must not materialize unrelated control/usage documents."""

import json

from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import SqlSessionStore, sessions


async def test_list_returns_ordered_owned_metadata_without_json_materialization():
    decoded = []

    def decode(value):
        decoded.append(len(value))
        return json.loads(value)

    engine = create_async_engine("sqlite+aiosqlite://", json_deserializer=decode)
    try:
        store = SqlSessionStore(engine)
        await store.setup()
        records = [
            SessionRecord(
                name,
                actor,
                "v2",
                "policy-1",
                name,
                run_id=name,
                created_at=date,
                user_request="List fixture",
            )
            for name, actor, date in (
                ("a", "owner", "2026-01-01"),
                ("b", "owner", "2026-01-01"),
                ("c", "owner", "2026-01-02"),
                ("d", "other", "2026-01-03"),
            )
        ]
        for record in records:
            await store.create(
                record, TokenBudget(limit=1000, budget_id=record.budget_id).snapshot()
            )
        async with engine.begin() as connection:
            await connection.execute(
                update(sessions).values(control={"role_data": {"history": "x" * 65536}})
            )
        decoded.clear()
        assert await store.list_for_actor("owner") == [
            records[2],
            records[1],
            records[0],
        ]
        assert decoded == [], "Listing unnecessarily decoded control/budget JSON"
        assert await store.list_for_actor("missing") == []
    finally:
        await engine.dispose()
