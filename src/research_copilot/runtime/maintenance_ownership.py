"""Target-local maintenance journal, independent of restored public tables.

Only explicit maintenance provisions this schema. Runtime admission is read-only.
The public-only joint backup deliberately excludes this target's operation history.
"""

import os
import re
import socket
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from research_copilot.runtime.maintenance import (
    DATABASE_ACCESS_KEY,
    MaintenanceBusy,
    MaintenanceLost,
)
from research_copilot.runtime.runtime_ownership import CONNECTED
from research_copilot.runtime.urls import to_checkpoint_url
from research_copilot.workspace import safe_component

SCHEMA = "rc_maintenance"
UNSETTLED = (
    "Unsettled maintenance operation; inspect rc maintenance-owners and explicitly "
    "recover-maintenance after stopping the old process and external tools"
)
PRESENT = "SELECT to_regnamespace('rc_maintenance')"
TABLES = """SELECT array_agg(c.relname::text ORDER BY c.relname)
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='rc_maintenance'"""
EXPECTED = ["operations", "operations_pkey", "protocol", "protocol_pkey"]
VERSION = "SELECT version FROM rc_maintenance.protocol ORDER BY version"
EXTRAS = """SELECT EXISTS (
    SELECT 1 FROM pg_depend d JOIN pg_namespace n ON d.refobjid=n.oid
    WHERE d.refclassid='pg_namespace'::regclass AND n.nspname='rc_maintenance'
    AND NOT (
        (d.classid='pg_class'::regclass AND d.objid IN (
            SELECT oid FROM pg_class WHERE relnamespace=n.oid
            AND relname IN ('protocol','protocol_pkey','operations','operations_pkey')
        )) OR
        (d.classid='pg_type'::regclass AND d.objid IN (
            SELECT oid FROM pg_type WHERE typnamespace=n.oid
            AND typname IN ('protocol','_protocol','operations','_operations')
        )) OR
        (d.classid='pg_constraint'::regclass AND d.objid IN (
            SELECT oid FROM pg_constraint WHERE connamespace=n.oid
            AND conname IN ('protocol_pkey','protocol_version_check','operations_pkey','operations_check')
        ))
    )
)"""
OPEN = "SELECT 1 FROM rc_maintenance.operations WHERE state='open' LIMIT 1"
DDL = (
    "CREATE SCHEMA rc_maintenance",
    "CREATE TABLE rc_maintenance.protocol (version integer PRIMARY KEY CHECK(version=1))",
    "INSERT INTO rc_maintenance.protocol VALUES (1)",
    """CREATE TABLE rc_maintenance.operations (
        id varchar(32) PRIMARY KEY,
        operation varchar(32) NOT NULL,
        hostname varchar(256) NOT NULL,
        process_id integer NOT NULL,
        backend_pid integer NOT NULL,
        backend_started_at timestamptz NOT NULL,
        started_at timestamptz NOT NULL,
        state varchar(16) NOT NULL,
        closed_at timestamptz,
        failed boolean,
        recovery_actor varchar(128),
        recovery_note text,
        CHECK ((state='open' AND closed_at IS NULL) OR
               (state IN ('closed','recovered') AND closed_at IS NOT NULL))
    )""",
)
INSERT = """INSERT INTO rc_maintenance.operations
    (id,operation,hostname,process_id,backend_pid,backend_started_at,started_at,state)
    SELECT %s,%s,%s,%s,pg_backend_pid(),backend_start,clock_timestamp(),'open'
    FROM pg_stat_activity WHERE pid=pg_backend_pid()"""
CLOSE = (
    "UPDATE rc_maintenance.operations o SET state='closed',closed_at=clock_timestamp(),failed=%s "
    "WHERE id=%s AND state='open' AND backend_pid=pg_backend_pid() AND " + CONNECTED
)


def _invalid():
    return ValueError("Unsupported or occupied rc_maintenance control schema")


def validate(connection):
    if connection.execute(PRESENT).fetchone()[0] is None:
        return False
    if connection.execute(TABLES).fetchone()[0] != EXPECTED:
        raise _invalid()
    if connection.execute(VERSION).fetchall() != [(1,)]:
        raise _invalid()
    if connection.execute(EXTRAS).fetchone()[0]:
        raise _invalid()
    return True


async def validate_async(connection):
    if (await (await connection.execute(PRESENT)).fetchone())[0] is None:
        return False
    if (await (await connection.execute(TABLES)).fetchone())[0] != EXPECTED:
        raise _invalid()
    if await (await connection.execute(VERSION)).fetchall() != [(1,)]:
        raise _invalid()
    if (await (await connection.execute(EXTRAS)).fetchone())[0]:
        raise _invalid()
    return True


def assert_settled(connection):
    if validate(connection) and connection.execute(OPEN).fetchone():
        raise MaintenanceBusy(UNSETTLED)


async def assert_settled_async(connection):
    if (
        await validate_async(connection)
        and await (await connection.execute(OPEN)).fetchone()
    ):
        raise MaintenanceBusy(UNSETTLED)


def _parameters(operation):
    if operation not in {"backup", "restore", "initialize", "bind_host", "maintenance"}:
        raise ValueError("Unknown maintenance operation")
    return uuid4().hex, operation, socket.gethostname(), os.getpid()


def begin(connection, operation):
    parameters = _parameters(operation)
    with connection.transaction():
        if not validate(connection):
            for statement in DDL:
                connection.execute(statement)
        connection.execute(INSERT, parameters)
    return parameters[0]


async def begin_async(connection, operation):
    parameters = _parameters(operation)
    async with connection.transaction():
        if not await validate_async(connection):
            for statement in DDL:
                await connection.execute(statement)
        await connection.execute(INSERT, parameters)
    return parameters[0]


def close(connection, identity, failed):
    if connection.execute(CLOSE, (failed, identity, DATABASE_ACCESS_KEY)).rowcount != 1:
        raise MaintenanceLost("Maintenance ownership could not be closed")


async def close_async(connection, identity, failed):
    result = await connection.execute(CLOSE, (failed, identity, DATABASE_ACCESS_KEY))
    if result.rowcount != 1:
        raise MaintenanceLost("Maintenance ownership could not be closed")


async def list_owners(database_url):
    # Diagnostic SELECTs are allowed even while an exclusive maintenance lock is held.
    async with await psycopg.AsyncConnection.connect(
        to_checkpoint_url(database_url), autocommit=True, connect_timeout=5
    ) as connection:
        await connection.execute("SET statement_timeout='5s'")
        await connection.execute("SET default_transaction_read_only=on")
        if not await validate_async(connection):
            return []
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                "SELECT o.*, " + CONNECTED + " AS connected "
                "FROM rc_maintenance.operations o WHERE state='open' "
                "ORDER BY started_at,id LIMIT 100",
                (DATABASE_ACCESS_KEY,),
            )
            return await cursor.fetchall()


async def recover_owner(database_url, owner_id, *, actor_id, note, owner_stopped):
    from research_copilot.runtime.maintenance import async_database_access

    if owner_stopped is not True:
        raise ValueError(
            "Old maintenance process and external tools must be confirmed stopped"
        )
    if not isinstance(owner_id, str) or not re.fullmatch(r"[a-f0-9]{32}", owner_id):
        raise ValueError("Invalid maintenance owner identity")
    safe_component(actor_id)
    if (
        len(actor_id) > 128
        or not isinstance(note, str)
        or not 1 <= len(note.strip()) <= 2000
    ):
        raise ValueError("Recovery actor and evidence are required")
    note = note.strip()
    async with async_database_access(
        database_url,
        exclusive=True,
        allow_unsettled=True,
        allow_maintenance_unsettled=True,
    ) as connection:
        if not await validate_async(connection):
            raise LookupError("Maintenance owner unavailable")
        async with connection.transaction():
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT * FROM rc_maintenance.operations WHERE id=%s FOR UPDATE",
                    (owner_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise LookupError("Maintenance owner unavailable")
                if row["state"] == "recovered":
                    if (
                        row["recovery_actor"] == actor_id
                        and row["recovery_note"] == note
                    ):
                        return row
                    raise ValueError(
                        "Maintenance owner already recovered with different evidence"
                    )
                if row["state"] != "open":
                    raise ValueError("Maintenance owner already closed normally")
                await cursor.execute(
                    "UPDATE rc_maintenance.operations SET state='recovered',closed_at=clock_timestamp(),"
                    "recovery_actor=%s,recovery_note=%s WHERE id=%s AND state='open' RETURNING *",
                    (actor_id, note, owner_id),
                )
                return await cursor.fetchone()
