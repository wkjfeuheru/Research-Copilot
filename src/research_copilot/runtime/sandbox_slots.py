"""Durable sandbox capacity: unknown external work never expires into a free slot."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError

from research_copilot.runtime.maintenance import MaintenanceBusy, MaintenanceLost
from research_copilot.runtime.runtime_ownership import owners
from research_copilot.runtime.task_completion import await_completion
from research_copilot.workspace import safe_component

logger = logging.getLogger(__name__)
metadata = MetaData()
claims = Table(
    "sandbox_slot_claims",
    metadata,
    Column("id", String(32), primary_key=True),
    Column("slot", Integer, nullable=False),
    Column("owner_id", String(32), nullable=False, index=True),
    Column("report_id", String(128), nullable=False),
    Column("dispatch_id", String(128), nullable=False),
    Column("execution_id", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True)),
    Column("recovery_actor", String(128)),
    Column("recovery_note", Text),
    CheckConstraint("slot IN (0,1)"),
    CheckConstraint(
        "(state='held' AND closed_at IS NULL) OR (state IN ('released','recovered') AND closed_at IS NOT NULL)"
    ),
)
Index(
    "sandbox_one_held_claim_per_slot",
    claims.c.slot,
    unique=True,
    postgresql_where=text("state='held'"),
    sqlite_where=text("state='held'"),
)


class SandboxSlotBusy(MaintenanceBusy):
    pass


@dataclass
class SlotLease:
    external_started: bool = False
    external_stopped: bool = False
    reserved: bool = False


class SandboxSlots:
    def __init__(self, engine, owner_id):
        if not re.fullmatch(r"[a-f0-9]{32}", owner_id):
            raise ValueError("Invalid sandbox owner identity")
        self.engine, self.owner_id = engine, owner_id

    async def available(self, slot):
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    select(claims.c.id).where(
                        claims.c.slot == slot,
                        claims.c.state == "held",
                    )
                )
            ).first()
        return row is None

    @asynccontextmanager
    async def hold(self, slot, report_id, dispatch_id, execution_id):
        if type(slot) is not int or slot not in (0, 1):
            raise ValueError("Invalid sandbox slot")
        safe_component(report_id)
        safe_component(dispatch_id)
        if not re.fullmatch(r"exec-[a-f0-9]{32}", execution_id):
            raise ValueError("Invalid sandbox execution identity")
        identity, lease = uuid4().hex, SlotLease()

        async def reserve():
            try:
                async with self.engine.begin() as connection:
                    owner = (
                        await connection.execute(
                            select(owners.c.id).where(
                                owners.c.id == self.owner_id,
                                owners.c.state == "open",
                            )
                        )
                    ).scalar_one_or_none()
                    if owner is None:
                        raise MaintenanceLost("Sandbox runtime owner is not open")
                    await connection.execute(
                        insert(claims).values(
                            id=identity,
                            slot=slot,
                            owner_id=self.owner_id,
                            report_id=report_id,
                            dispatch_id=dispatch_id,
                            execution_id=execution_id,
                            state="held",
                            started_at=datetime.now(timezone.utc),
                        )
                    )
                lease.reserved = True
            except IntegrityError:
                async with self.engine.connect() as connection:
                    held = (
                        await connection.execute(
                            select(claims.c.id).where(
                                claims.c.slot == slot,
                                claims.c.state == "held",
                            )
                        )
                    ).first()
                if held:
                    raise SandboxSlotBusy(
                        "Sandbox slot has unsettled external work; inspect rc sandbox-claims and recover its runtime owner after confirming external execution stopped"
                    ) from None
                raise

        async def release():
            async with self.engine.begin() as connection:
                result = await connection.execute(
                    update(claims)
                    .where(
                        claims.c.id == identity,
                        claims.c.owner_id == self.owner_id,
                        claims.c.state == "held",
                    )
                    .values(state="released", closed_at=datetime.now(timezone.utc))
                )
                if result.rowcount != 1:
                    raise MaintenanceLost("Sandbox slot ownership changed")

        failed = False
        try:
            await await_completion(reserve())
            yield lease
            if lease.external_started and not lease.external_stopped:
                raise MaintenanceLost("Sandbox cleanup is unknown; capacity retained")
        except BaseException:
            failed = True
            raise
        finally:
            if lease.reserved and (
                not lease.external_started or lease.external_stopped
            ):
                try:
                    await await_completion(release())
                except BaseException:
                    if not failed:
                        raise
                    logger.error(
                        "Sandbox capacity release result unknown; inspect the durable claim"
                    )


async def list_claims(database_url):
    from psycopg.rows import dict_row
    from research_copilot.runtime.maintenance import async_database_access

    async with async_database_access(database_url) as connection:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                "SELECT * FROM public.sandbox_slot_claims WHERE state='held' ORDER BY slot"
            )
            return await cursor.fetchall()
