import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from research_copilot.runtime.storage import SqlSessionStore
from research_copilot.runtime.migrations import SCHEMA_VERSION


async def test_v5_runtime_owner_upgrade_requires_explicit_migration(tmp_path):
    from sqlalchemy import inspect, select
    from research_copilot.runtime.runtime_ownership import metadata, owners

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'owners.db'}")
    store = SqlSessionStore(engine)
    try:
        await store.setup()
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.execute(
                text("DELETE FROM rc_schema_migrations WHERE version>=6")
            )
            before = (
                await connection.execute(
                    text("SELECT * FROM rc_schema_migrations ORDER BY version")
                )
            ).all()
        with pytest.raises(ValueError, match="init-db"):
            await store.check_schema()
        async with engine.connect() as connection:
            assert not await connection.run_sync(
                lambda sync: inspect(sync).has_table(owners.name)
            )
        await store.setup()
        async with engine.connect() as connection:
            assert not (await connection.execute(select(owners))).all()
            assert (
                await connection.execute(
                    text(
                        "SELECT * FROM rc_schema_migrations WHERE version<6 ORDER BY version"
                    )
                )
            ).all() == before
    finally:
        await engine.dispose()


async def test_v6_sandbox_capacity_upgrade_is_explicit_and_checks_unique_index(
    tmp_path,
):
    from sqlalchemy import inspect
    from research_copilot.runtime.sandbox_slots import metadata

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'sandbox-upgrade.db'}"
    )
    store = SqlSessionStore(engine)
    try:
        await store.setup()
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.execute(
                text("DELETE FROM rc_schema_migrations WHERE version>=7")
            )
            original = (
                await connection.execute(
                    text("SELECT * FROM rc_schema_migrations ORDER BY version")
                )
            ).all()
        with pytest.raises(ValueError, match="init-db"):
            await store.check_schema()
        async with engine.connect() as connection:
            assert not await connection.run_sync(
                lambda conn: inspect(conn).has_table("sandbox_slot_claims")
            )
        await store.setup()
        async with engine.begin() as connection:
            assert (
                await connection.execute(
                    text(
                        "SELECT * FROM rc_schema_migrations WHERE version<7 ORDER BY version"
                    )
                )
            ).all() == original
            await connection.execute(text("DROP INDEX sandbox_one_held_claim_per_slot"))
        with pytest.raises(ValueError, match="unique sandbox"):
            await store.check_schema()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_schema_upgrade_is_idempotent_and_startup_only_checks(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}")
    store = SqlSessionStore(engine)
    try:
        with pytest.raises(ValueError, match="init-db"):
            await store.check_schema()
        await store.setup()
        await store.setup()
        await store.check_schema()
        async with engine.begin() as conn:
            assert (
                await conn.execute(
                    text("SELECT version FROM rc_schema_migrations ORDER BY version")
                )
            ).scalars().all() == list(range(1, SCHEMA_VERSION + 1))
            await conn.execute(
                text(
                    "UPDATE rc_schema_migrations SET checksum='tampered' WHERE version=1"
                )
            )
        with pytest.raises(ValueError, match="checksum"):
            await store.setup()
    finally:
        await engine.dispose()


async def test_v4_binding_upgrade_is_explicit_and_preserves_prior_history(tmp_path):
    from sqlalchemy import inspect, select
    from research_copilot.runtime.host_binding import metadata, host_binding
    from research_copilot.runtime.run_queue import run_queue_settings

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'binding-upgrade.db'}"
    )
    store = SqlSessionStore(engine)
    try:
        await store.setup()
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
            await conn.execute(
                text("DELETE FROM rc_schema_migrations WHERE version>=5")
            )
            await conn.execute(run_queue_settings.update().values(max_active=7))
            before = (
                await conn.execute(
                    text("SELECT * FROM rc_schema_migrations ORDER BY version")
                )
            ).all()
        with pytest.raises(ValueError, match="init-db"):
            await store.check_schema()
        async with engine.connect() as conn:
            assert not await conn.run_sync(
                lambda sync: inspect(sync).has_table(host_binding.name)
            )
        await store.setup()
        await store.check_schema()
        async with engine.connect() as conn:
            assert not (await conn.execute(select(host_binding))).all()
            assert (
                await conn.execute(select(run_queue_settings.c.max_active))
            ).scalar_one() == 7
            assert (
                await conn.execute(
                    text(
                        "SELECT * FROM rc_schema_migrations WHERE version<5 ORDER BY version"
                    )
                )
            ).all() == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_future_schema_rejected_without_changes(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'future.db'}")
    store = SqlSessionStore(engine)
    try:
        await store.setup()
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO rc_schema_migrations VALUES (99, 'future', 'future')")
            )
        with pytest.raises(ValueError, match="newer|sequence"):
            await store.setup()
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT count(*) FROM rc_schema_migrations"))
            ).scalar_one() == SCHEMA_VERSION + 1
    finally:
        await engine.dispose()


async def test_v2_startup_requires_explicit_queue_upgrade(tmp_path):
    from research_copilot.runtime.run_queue import metadata, run_queue_settings
    from sqlalchemy import select

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}")
    store = SqlSessionStore(engine)
    try:
        await store.setup()
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
            await conn.execute(
                text("DELETE FROM rc_schema_migrations WHERE version>=3")
            )
            original = (
                await conn.execute(
                    text("SELECT * FROM rc_schema_migrations ORDER BY version")
                )
            ).all()
        with pytest.raises(ValueError, match="init-db"):
            await store.check_schema()
        await store.setup()
        await store.check_schema()
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT * FROM rc_schema_migrations WHERE version < 3 ORDER BY version"
                    )
                )
            ).all() == original
            settings = (await conn.execute(select(run_queue_settings))).mappings().one()
            assert settings["max_active"] == 3 and settings["max_pending"] == 100
        async with engine.begin() as conn:
            await conn.execute(run_queue_settings.delete())
        with pytest.raises(ValueError, match="queue configuration"):
            await store.check_schema()
    finally:
        await engine.dispose()


async def test_v3_recovery_upgrade_is_explicit_and_preserves_queue(tmp_path):
    from sqlalchemy import inspect, select
    from research_copilot.runtime.run_queue import run_queue_settings
    from research_copilot.runtime.run_recovery import metadata

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'recovery-upgrade.db'}"
    )
    store = SqlSessionStore(engine)
    try:
        await store.setup()
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
            await conn.execute(
                text("DELETE FROM rc_schema_migrations WHERE version>=4")
            )
            await conn.execute(run_queue_settings.update().values(max_active=7))
            before = (
                await conn.execute(
                    text("SELECT * FROM rc_schema_migrations ORDER BY version")
                )
            ).all()
        with pytest.raises(ValueError, match="init-db"):
            await store.check_schema()
        async with engine.connect() as conn:
            assert not await conn.run_sync(
                lambda sync: inspect(sync).has_table("research_run_recoveries")
            )
        await store.setup()
        await store.check_schema()
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT * FROM rc_schema_migrations WHERE version<=3 ORDER BY version"
                    )
                )
            ).all() == before
            assert (
                await conn.execute(select(run_queue_settings.c.max_active))
            ).scalar_one() == 7
    finally:
        await engine.dispose()
