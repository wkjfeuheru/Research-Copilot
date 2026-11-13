"""Independent per-attempt retrospective usage, persisted before response parsing."""

from copy import deepcopy

from sqlalchemy import select, update


async def record_attempt(engine, claim, *, status, usage=None):
    from research_copilot.runtime.experience import (
        retrospective_jobs,
        StaleClaim,
        _live_claim,
    )

    async with engine.begin() as connection:
        row = (
            (
                await connection.execute(
                    select(retrospective_jobs)
                    .where(_live_claim(connection, claim))
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise StaleClaim("Retrospective usage claim is stale")
        previous = row["usage"] or {}
        attempts = deepcopy(previous.get("attempts", []))
        attempt = next(
            (item for item in attempts if item["attempt_id"] == claim["claim_token"]),
            None,
        )
        if attempt is None:
            attempt = {
                "attempt_id": claim["claim_token"],
                "attempt_number": claim["attempts"],
                "usage": {},
            }
            attempts.append(attempt)
        attempt["status"] = status
        if usage is not None:
            attempt["usage"] = deepcopy(usage)
        ledger = rebuild_ledger(previous, attempts)
        result = await connection.execute(
            update(retrospective_jobs)
            .where(_live_claim(connection, claim))
            .values(usage=ledger)
        )
        if result.rowcount != 1:
            raise StaleClaim("Retrospective usage claim expired during settlement")


def rebuild_ledger(previous, attempts):
    legacy = previous.get(
        "legacy_usage", previous if "attempts" not in previous else {}
    )
    records = [item["usage"] for item in attempts] + ([legacy] if legacy else [])
    totals = {
        key: sum(
            record.get(key, 0)
            for record in records
            if type(record.get(key)) is int and record[key] >= 0
        )
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }
    unknown = sum(
        not (
            all(
                type(record.get(key)) is int and record[key] >= 0
                for key in ("input_tokens", "output_tokens")
            )
            and record.get("total_tokens")
            == record["input_tokens"] + record["output_tokens"]
            and not record.get("usage_provisional")
        )
        for record in records
    )
    ledger = {
        **deepcopy(previous),
        **totals,
        "attempts": attempts,
        "unknown_attempts": unknown,
        "usage_verified": unknown == 0,
    }
    if legacy:
        ledger["legacy_usage"] = legacy
    return ledger
