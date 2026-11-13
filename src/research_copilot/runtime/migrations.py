"""Append-only application schema history; LangGraph owns its own migrations.

Run only through explicit store.setup()/rc init-db with application writers
stopped. Startup calls check() and never performs DDL.
"""

from hashlib import sha256

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    insert,
    select,
    text,
)

metadata = MetaData()
history = Table(
    "rc_schema_migrations",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("name", String(128), nullable=False),
    Column("checksum", String(64), nullable=False),
)
MIGRATIONS = (
    (1, "sessions-v2-legacy-rename-user_request-created_at"),
    (2, "retrospective_jobs-experiences-experience_usages-v1"),
    (3, "research-run-queue-admission-v1"),
    (4, "research-run-recovery-audit-v1"),
    (5, "host-workspace-configuration-binding-v1"),
    (6, "durable-runtime-ownership-v1"),
    (7, "durable-sandbox-slot-claims-v1"),
)
SCHEMA_VERSION = len(MIGRATIONS)


async def _applied(connection):
    names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
    if history.name not in names:
        return []
    rows = (
        (await connection.execute(select(history).order_by(history.c.version)))
        .mappings()
        .all()
    )
    if [row["version"] for row in rows] != list(range(1, len(rows) + 1)) or len(
        rows
    ) > SCHEMA_VERSION:
        raise ValueError(
            "Unknown/newer schema migration sequence; use the matching application version"
        )
    for row, (_, name) in zip(rows, MIGRATIONS):
        if row["name"] != name or row["checksum"] != sha256(name.encode()).hexdigest():
            raise ValueError("Schema migration checksum mismatch")
    return rows


async def check(connection):
    if len(await _applied(connection)) != SCHEMA_VERSION:
        raise ValueError(
            "Schema upgrade required: stop writers, back up, then run rc init-db"
        )
    from research_copilot.runtime.storage import sessions
    from research_copilot.runtime.run_recovery import metadata as recovery_metadata
    from research_copilot.runtime.host_binding import metadata as host_metadata
    from research_copilot.runtime.runtime_ownership import metadata as owner_metadata
    from research_copilot.runtime.sandbox_slots import metadata as sandbox_metadata
    from research_copilot.runtime.experience import metadata as experience_metadata
    from research_copilot.runtime.run_queue import (
        metadata as queue_metadata,
        run_queue_settings,
    )

    def verify(conn):
        inspector = inspect(conn)
        for table in [
            sessions,
            *experience_metadata.tables.values(),
            *queue_metadata.tables.values(),
            *recovery_metadata.tables.values(),
            *host_metadata.tables.values(),
            *owner_metadata.tables.values(),
            *sandbox_metadata.tables.values(),
        ]:
            if not inspector.has_table(table.name):
                raise ValueError("Schema missing table: " + table.name)
            columns = {item["name"] for item in inspector.get_columns(table.name)}
            if not set(table.columns.keys()) <= columns:
                raise ValueError("Schema missing columns: " + table.name)
        indexes = inspector.get_indexes("sandbox_slot_claims")
        if not any(
            item["name"] == "sandbox_one_held_claim_per_slot"
            and item["unique"]
            and item["column_names"] == ["slot"]
            for item in indexes
        ):
            raise ValueError("Schema missing unique sandbox capacity index")

    await connection.run_sync(verify)
    settings = (await connection.execute(select(run_queue_settings))).mappings().all()
    if (
        len(settings) != 1
        or settings[0]["id"] != 1
        or settings[0]["max_active"] <= 0
        or settings[0]["max_pending"] <= 0
    ):
        raise ValueError("Invalid persisted research queue configuration")


async def migrate(store, connection):
    if connection.dialect.name == "postgresql":
        await connection.execute(text("SELECT pg_advisory_xact_lock(725231109)"))
    applied = await _applied(connection)
    await connection.run_sync(metadata.create_all)
    for version, name in MIGRATIONS[len(applied) :]:
        if version == 1:
            from research_copilot.runtime.storage import metadata as session_metadata
            from research_copilot.runtime.identity import (
                LEGACY_SESSION_TABLE_NAME,
                SESSION_TABLE_NAME,
            )

            names = await connection.run_sync(
                lambda conn: inspect(conn).get_table_names()
            )
            if LEGACY_SESSION_TABLE_NAME in names and SESSION_TABLE_NAME in names:
                raise ValueError(
                    "Both legacy and current session tables exist; resolve ownership before migration"
                )
            await store._rename_legacy_session_table(connection)
            await connection.run_sync(session_metadata.create_all)
            await store._ensure_session_columns(connection)
        elif version == 2:
            await store.experience.setup(connection)
        elif version == 3:
            from research_copilot.runtime.run_queue import setup

            await setup(connection)
        elif version == 4:
            from research_copilot.runtime.run_recovery import (
                metadata as recovery_metadata,
            )

            await connection.run_sync(recovery_metadata.create_all)
        elif version == 5:
            from research_copilot.runtime.host_binding import metadata as host_metadata

            await connection.run_sync(host_metadata.create_all)
        elif version == 6:
            from research_copilot.runtime.runtime_ownership import (
                metadata as owner_metadata,
            )

            await connection.run_sync(owner_metadata.create_all)
        elif version == 7:
            from research_copilot.runtime.sandbox_slots import (
                metadata as sandbox_metadata,
            )

            await connection.run_sync(sandbox_metadata.create_all)
        await connection.execute(
            insert(history).values(
                version=version, name=name, checksum=sha256(name.encode()).hexdigest()
            )
        )
    await check(connection)
