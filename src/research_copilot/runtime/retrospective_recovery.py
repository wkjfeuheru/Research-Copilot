"""Audited operator decisions for the independent retrospective ledger."""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select, update

from research_copilot.runtime.retrospective_usage import rebuild_ledger


class RecoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["reconcile", "authorize_retry"]
    operation_id: str = Field(pattern=r"^[A-Za-z0-9-]{16,64}$")
    expected_revision: StrictInt = Field(ge=0)
    note: str = Field(min_length=1, max_length=2000)
    attempt_id: str | None = None
    input_tokens: StrictInt | None = Field(default=None, ge=0, le=9007199254740991)
    output_tokens: StrictInt | None = Field(default=None, ge=0, le=9007199254740991)
    acknowledge_duplicate_charge: StrictBool = False

    @field_validator("note")
    @classmethod
    def nonblank_note(cls, value):
        if not value.strip():
            raise ValueError("核实依据或重试理由不能为空")
        return value.strip()

    @model_validator(mode="after")
    def separate_decisions(self):
        if self.action == "reconcile":
            if (
                not self.attempt_id
                or self.input_tokens is None
                or self.output_tokens is None
                or self.acknowledge_duplicate_charge
            ):
                raise ValueError("核实需要尝试 ID、输入与输出用量，不包含重试授权")
            if self.input_tokens + self.output_tokens > 9007199254740991:
                raise ValueError("总用量超出界面可精确表示的整数范围")
        elif not self.acknowledge_duplicate_charge or any(
            v is not None
            for v in (self.attempt_id, self.input_tokens, self.output_tokens)
        ):
            raise ValueError("重试须单独确认可能重复收费，不同时修改历史用量")
        return self


class RecoveryConflict(ValueError):
    pass


def retry_is_authorized(usage, next_attempt):
    authorization = (usage or {}).get("retry_authorization", {})
    return authorization.get(
        "attempt_number"
    ) == next_attempt and not authorization.get("consumed_by")


async def recover_job(engine, actor_id, report_id, job_id, request: RecoveryRequest):
    from research_copilot.runtime.experience import (
        retrospective_jobs,
        _job_dict,
        _lease_clock,
        _iso,
    )

    request_hash = hashlib.sha256(
        json.dumps(request.model_dump(), sort_keys=True).encode()
    ).hexdigest()
    async with engine.begin() as connection:
        row = (
            (
                await connection.execute(
                    select(retrospective_jobs)
                    .where(
                        retrospective_jobs.c.id == job_id,
                        retrospective_jobs.c.report_id == report_id,
                        retrospective_jobs.c.actor_id == actor_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise LookupError("复盘任务不存在")
        previous = row["usage"] or {}
        audit = deepcopy(previous.get("recovery_audit", []))
        for event in audit:
            if event["operation_id"] == request.operation_id:
                if (
                    event["request_hash"] != request_hash
                    or event["actor_id"] != actor_id
                ):
                    raise RecoveryConflict("同一操作标识不能用于不同决定")
                return _job_dict(row)
        revision = previous.get("recovery_revision", 0)
        if revision != request.expected_revision:
            raise RecoveryConflict("复盘记录已更新，请刷新后重新核对")
        if row["status"] not in ("failed", "completed"):
            raise RecoveryConflict("等待中或运行中的复盘不能人工改写")
        ledger = rebuild_ledger(previous, deepcopy(previous.get("attempts", [])))
        event = {
            "operation_id": request.operation_id,
            "request_hash": request_hash,
            "actor_id": actor_id,
            "action": request.action,
            "note": request.note,
            "attempt_id": request.attempt_id,
        }
        if request.action == "reconcile":
            assert (
                request.input_tokens is not None and request.output_tokens is not None
            )
            attempts = deepcopy(previous.get("attempts", []))
            attempt = next(
                (a for a in attempts if a["attempt_id"] == request.attempt_id), None
            )
            if attempt is None:
                raise ValueError("尝试 ID 不属于此复盘的独立账本")
            event["previous_usage"] = deepcopy(attempt["usage"])
            attempt["usage"] = {
                **attempt["usage"],
                "input_tokens": request.input_tokens,
                "output_tokens": request.output_tokens,
                "total_tokens": request.input_tokens + request.output_tokens,
                "usage_provisional": False,
                "verified_by": actor_id,
            }
            event["verified_usage"] = deepcopy(attempt["usage"])
            ledger = rebuild_ledger(previous, attempts)
        else:
            if row["status"] != "failed":
                raise RecoveryConflict("只有失败的复盘可以授权再次尝试")
            ledger["retry_authorization"] = {
                "operation_id": request.operation_id,
                "attempt_number": row["attempts"] + 1,
                "consumed_by": None,
            }
        clock = _lease_clock(connection)
        timestamp = (
            clock
            if isinstance(clock, datetime)
            else await connection.scalar(select(clock))
        )
        event["at"] = _iso(timestamp)
        ledger["recovery_audit"] = [*audit, event]
        ledger["recovery_revision"] = revision + 1
        values = {"usage": ledger, "updated_at": _lease_clock(connection)}
        if request.action == "authorize_retry":
            values.update(
                status="pending",
                max_attempts=row["attempts"] + 1,
                available_at=_lease_clock(connection),
                locked_until=None,
                claim_token=None,
                last_error=None,
            )
        # PostgreSQL locks the row; this comparison also fences SQLite callers.
        result = await connection.execute(
            update(retrospective_jobs)
            .where(
                retrospective_jobs.c.id == job_id,
                retrospective_jobs.c.status == row["status"],
                retrospective_jobs.c.attempts == row["attempts"],
                func.coalesce(
                    retrospective_jobs.c.usage["recovery_revision"].as_integer(), 0
                )
                == revision,
            )
            .values(**values)
            .returning(*retrospective_jobs.c)
        )
        changed = result.mappings().first()
        if changed is None:
            raise RecoveryConflict("复盘记录已更新，请刷新后重新核对")
        return _job_dict(changed)
