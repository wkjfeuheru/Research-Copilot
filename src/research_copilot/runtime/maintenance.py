"""Cooperating database lifespans exclude offline maintenance.

PostgreSQL releases this session lock on disconnect. Runtime and target-local
maintenance journals retain admission barriers after loss. RuntimeWatch monitors
tracked running scopes; maintenance recovery requires old tools to be stopped.
Never reconnect this owning connection.
https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS
"""

from contextlib import asynccontextmanager, contextmanager
import logging

import psycopg

from research_copilot.runtime.urls import to_checkpoint_url

logger = logging.getLogger(__name__)
# Separate from schema migration's transaction lock (725231109): maintenance
# owns this session connection while DDL runs on a different connection.
DATABASE_ACCESS_KEY = 725231110


class MaintenanceBusy(RuntimeError):
    """An incompatible runtime or maintenance owner is still connected."""


class MaintenanceLost(RuntimeError):
    """The owning connection/lock was lost; no automatic reacquisition is safe."""


def _sql(exclusive):
    suffix = "" if exclusive else "_shared"
    return (
        f"SELECT pg_try_advisory_lock{suffix}(%s)",
        f"SELECT pg_advisory_unlock{suffix}(%s)",
    )


def _lost(failed):
    if not failed:
        raise MaintenanceLost(
            "Database maintenance ownership lost; inspect running writers and preserved evidence"
        ) from None
    # Preserve the original error, including cancellation, without leaking DSNs.
    logger.error("Database maintenance ownership lost during failed operation")


@asynccontextmanager
async def async_database_access(
    database_url,
    *,
    exclusive=False,
    allow_unsettled=False,
    operation=None,
    allow_maintenance_unsettled=False,
):
    if allow_unsettled and not exclusive:
        raise ValueError("Runtime recovery requires exclusive access")
    if (operation or allow_maintenance_unsettled) and not exclusive:
        raise ValueError("Maintenance ownership requires exclusive access")
    if operation and allow_maintenance_unsettled:
        raise ValueError("Cannot start maintenance while recovering its ownership")
    acquire, release = _sql(exclusive)
    async with await psycopg.AsyncConnection.connect(
        to_checkpoint_url(database_url),
        autocommit=True,
        connect_timeout=5,
        application_name="research-copilot-database-access",
    ) as connection:
        await connection.execute("SET statement_timeout = '5s'")
        row = await (
            await connection.execute(acquire, (DATABASE_ACCESS_KEY,))
        ).fetchone()
        if not row[0]:
            raise MaintenanceBusy(
                "Database maintenance conflicts with an active runtime or maintenance owner"
            )
        from research_copilot.runtime import maintenance_ownership as journal

        if allow_maintenance_unsettled:
            await journal.validate_async(connection)
        else:
            await journal.assert_settled_async(connection)
        if exclusive and not allow_unsettled:
            from research_copilot.runtime.runtime_ownership import assert_settled_async

            await assert_settled_async(connection)
        identity = (
            await journal.begin_async(connection, operation) if operation else None
        )
        failed = False
        try:
            yield connection
        except BaseException:
            failed = True
            raise
        finally:
            settled = True
            if identity is not None:
                try:
                    await journal.close_async(connection, identity, failed)
                except Exception:
                    settled = False
            try:
                row = await (
                    await connection.execute(release, (DATABASE_ACCESS_KEY,))
                ).fetchone()
                retained = row[0]
            except Exception:
                retained = False
            if not retained or not settled:
                _lost(failed)


@contextmanager
def maintenance_connection(database_url, *, operation="maintenance"):
    acquire, release = _sql(True)
    with psycopg.connect(
        to_checkpoint_url(database_url),
        autocommit=True,
        connect_timeout=5,
        application_name="research-copilot-database-maintenance",
    ) as connection:
        connection.execute("SET statement_timeout = '5s'")
        if not connection.execute(acquire, (DATABASE_ACCESS_KEY,)).fetchone()[0]:
            raise MaintenanceBusy(
                "Database maintenance conflicts with an active runtime or maintenance owner"
            )
        from research_copilot.runtime.runtime_ownership import assert_settled
        from research_copilot.runtime import maintenance_ownership as journal

        journal.assert_settled(connection)
        assert_settled(connection)
        identity = journal.begin(connection, operation)
        failed = False
        try:
            yield connection
        except BaseException:
            failed = True
            raise
        finally:
            settled = True
            try:
                journal.close(connection, identity, failed)
            except Exception:
                settled = False
            try:
                retained = connection.execute(
                    release, (DATABASE_ACCESS_KEY,)
                ).fetchone()[0]
            except Exception:
                retained = False
            if not retained or not settled:
                _lost(failed)
