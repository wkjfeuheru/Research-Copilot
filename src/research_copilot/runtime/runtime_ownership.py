"""Durable runtime lifetime records; never infer stopped external work from time."""

import os
import re
import socket
from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    CheckConstraint,
)

from research_copilot.runtime.maintenance import (
    DATABASE_ACCESS_KEY,
    MaintenanceBusy,
    MaintenanceLost,
)
from research_copilot.workspace import safe_component

metadata = MetaData()
owners = Table(
    "runtime_owners",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("hostname", String(256), nullable=False),
    Column("process_id", Integer, nullable=False),
    Column("backend_pid", Integer, nullable=False),
    Column("backend_started_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("state", String(16), nullable=False, index=True),
    Column("closed_at", DateTime(timezone=True)),
    Column("recovery_actor", String(128)),
    Column("recovery_note", Text),
    CheckConstraint(
        "(state='open' AND closed_at IS NULL) OR (state IN ('closed','recovered') AND closed_at IS NOT NULL)"
    ),
)

# PID alone can be reused. Match backend birth and this database's exact lock.
CONNECTED = """EXISTS (
    SELECT 1 FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid
    WHERE a.pid=o.backend_pid AND a.backend_start=o.backend_started_at
      AND l.locktype='advisory' AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
      AND l.classid=0 AND l.objid=%s AND l.objsubid=1 AND l.granted
      AND l.mode IN ('ShareLock','ExclusiveLock')
)"""
UNSETTLED = "Unsettled runtime owners; inspect rc runtime-owners and explicitly recover-runtime after stopping old processes and external execution"


def assert_settled(connection):
    if connection.execute("SELECT to_regclass('public.runtime_owners')").fetchone()[0]:
        if connection.execute(
            "SELECT 1 FROM public.runtime_owners WHERE state='open' LIMIT 1"
        ).fetchone():
            raise MaintenanceBusy(UNSETTLED)


async def assert_settled_async(connection):
    if (
        await (
            await connection.execute("SELECT to_regclass('public.runtime_owners')")
        ).fetchone()
    )[0]:
        if await (
            await connection.execute(
                "SELECT 1 FROM public.runtime_owners WHERE state='open' LIMIT 1"
            )
        ).fetchone():
            raise MaintenanceBusy(UNSETTLED)


async def assert_connected(connection):
    if await (
        await connection.execute(
            "SELECT o.id FROM public.runtime_owners o WHERE state='open' AND NOT "
            + CONNECTED
            + " LIMIT 1",
            (DATABASE_ACCESS_KEY,),
        )
    ).fetchone():
        raise MaintenanceBusy(UNSETTLED)


async def register_owner(connection):
    await assert_connected(connection)
    identity = uuid4().hex
    await connection.execute(
        "INSERT INTO public.runtime_owners (id,hostname,process_id,backend_pid,backend_started_at,started_at,state) "
        "SELECT %s,%s,%s,pg_backend_pid(),backend_start,clock_timestamp(),'open' FROM pg_stat_activity WHERE pid=pg_backend_pid()",
        (identity, socket.gethostname(), os.getpid()),
    )
    return identity


async def close_owner(connection, identity):
    try:
        if await (
            await connection.execute(
                "SELECT id FROM public.sandbox_slot_claims WHERE owner_id=%s AND state='held' LIMIT 1",
                (identity,),
            )
        ).fetchone():
            raise MaintenanceLost(
                "Unsettled sandbox capacity; runtime owner retained for recovery"
            )
        result = await connection.execute(
            "UPDATE public.runtime_owners SET state='closed',closed_at=clock_timestamp() "
            "WHERE id=%s AND state='open' AND backend_pid=pg_backend_pid()",
            (identity,),
        )
        if result.rowcount == 1:
            return
    except Exception:
        pass
    raise MaintenanceLost(
        "Runtime ownership could not be closed; explicit inspection and recovery required"
    )


async def list_owners(database_url):
    from research_copilot.runtime.maintenance import async_database_access
    from psycopg.rows import dict_row

    async with async_database_access(database_url) as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                "SELECT o.*, "
                + CONNECTED
                + " AS connected FROM public.runtime_owners o "
                "WHERE state='open' ORDER BY started_at,id LIMIT 100",
                (DATABASE_ACCESS_KEY,),
            )
            return await cursor.fetchall()


async def recover_owner(database_url, owner_id, *, actor_id, note, owner_stopped):
    from research_copilot.runtime.maintenance import async_database_access
    from psycopg.rows import dict_row

    if owner_stopped is not True:
        raise ValueError("Old process and external execution must be confirmed stopped")
    if not isinstance(owner_id, str) or not re.fullmatch(r"[a-f0-9]{32}", owner_id):
        raise ValueError("Invalid runtime owner identity")
    safe_component(actor_id)
    if (
        len(actor_id) > 128
        or not isinstance(note, str)
        or not 1 <= len(note.strip()) <= 2000
    ):
        raise ValueError("Recovery actor and evidence are required")
    note = note.strip()
    async with async_database_access(
        database_url, exclusive=True, allow_unsettled=True
    ) as connection:
        async with connection.transaction():
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT * FROM public.runtime_owners WHERE id=%s FOR UPDATE",
                    (owner_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise LookupError("Runtime owner unavailable")
                if row["state"] == "recovered":
                    if (
                        row["recovery_actor"] == actor_id
                        and row["recovery_note"] == note
                    ):
                        return row
                    raise ValueError(
                        "Runtime owner already recovered with different evidence"
                    )
                if row["state"] != "open":
                    raise ValueError("Runtime owner already closed normally")
                # Recovery's explicit confirmation covers this owner's external
                # processes. Preserve each capacity receipt in the same transaction.
                await cursor.execute("SELECT to_regclass('public.sandbox_slot_claims')")
                if (await cursor.fetchone())["to_regclass"]:
                    await cursor.execute(
                        "UPDATE public.sandbox_slot_claims SET state='recovered',closed_at=clock_timestamp(),recovery_actor=%s,recovery_note=%s WHERE owner_id=%s AND state='held'",
                        (actor_id, note, owner_id),
                    )
                await cursor.execute(
                    "UPDATE public.runtime_owners SET state='recovered',closed_at=clock_timestamp(),recovery_actor=%s,recovery_note=%s "
                    "WHERE id=%s AND state='open' RETURNING *",
                    (actor_id, note, owner_id),
                )
                return await cursor.fetchone()
