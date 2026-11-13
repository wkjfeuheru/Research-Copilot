"""Durable atomic call/token reservations; unknown billing is never a refund."""

import asyncio
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4


class BudgetExceeded(RuntimeError):
    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.details = details or {}


class TokenBudget:
    def __init__(
        self,
        *,
        limit,
        budget_id,
        charged_tokens=0,
        unknown_requests=0,
        pending=None,
        persist=None,
        known_actual_tokens=None,
        unknown_reserved_tokens=None,
        call_limit=None,
        calls=0,
        stage_limits=None,
        stages=None,
        requests=None,
        prices=None,
        estimated_cost=None,
        revision=0,
    ):
        if (
            limit <= 0
            or charged_tokens < 0
            or (call_limit is not None and call_limit <= 0)
        ):
            raise ValueError("Invalid budget")
        self.limit, self.budget_id = limit, budget_id
        self.charged_tokens, self.unknown_requests = charged_tokens, unknown_requests
        # Legacy unknown charges cannot be relabelled as verified actual usage.
        self.known_actual_tokens = (
            (charged_tokens if not unknown_requests else 0)
            if known_actual_tokens is None
            else known_actual_tokens
        )
        self.unknown_reserved_tokens = (
            charged_tokens - self.known_actual_tokens
            if unknown_reserved_tokens is None
            else unknown_reserved_tokens
        )
        self.call_limit, self.calls = call_limit, calls
        self.stage_limits, self.stages = (
            deepcopy(stage_limits or {}),
            deepcopy(stages or {}),
        )
        self.requests, self.prices = deepcopy(requests or {}), deepcopy(prices or {})
        for rates in self.prices.values():
            if not rates.get("version"):
                raise ValueError("Price configuration needs a version")
            for key in ("input", "cache_read", "output"):
                value = Decimal(str(rates[key]))
                if not value.is_finite() or value < 0:
                    raise ValueError(
                        "Price configuration must be finite and nonnegative"
                    )
        self.estimated_cost = estimated_cost
        self.revision = revision
        self._persistence_failed = False
        self._pending = dict(pending or {})
        self._restored_pending = set(self._pending)
        for key, tokens in self._pending.items():
            self.requests.setdefault(
                key, {"stage": "supervisor", "reserved": tokens, "sent": True}
            )
        self._lock, self._persist = asyncio.Lock(), persist

    def snapshot(self):
        return deepcopy(
            {
                "budget_id": self.budget_id,
                "limit": self.limit,
                "charged_tokens": self.charged_tokens,
                "unknown_requests": self.unknown_requests,
                "pending": self._pending,
                "known_actual_tokens": self.known_actual_tokens,
                "unknown_reserved_tokens": self.unknown_reserved_tokens,
                "call_limit": self.call_limit,
                "calls": self.calls,
                "stage_limits": self.stage_limits,
                "stages": self.stages,
                "requests": self.requests,
                "prices": self.prices,
                "estimated_cost": self.estimated_cost,
                "revision": self.revision,
            }
        )

    def remaining_stage_calls(self, stage):
        """Calls left in a stage's cap; None when no per-stage limits are configured."""
        caps = self.stage_limits.get(stage)
        if not caps:
            return None
        used = self.stages.get(stage, {}).get("calls", 0)
        return max(0, caps.get("calls", 0) - used)

    def status(self):
        used = self.charged_tokens + sum(self._pending.values())
        return {
            k: v for k, v in self.snapshot().items() if k not in {"requests", "prices"}
        } | {
            "remaining_tokens": max(0, self.limit - used),
            "soft_warning": used >= self.limit * 0.8,
            "usage_verified": not self.unknown_requests
            and not self._pending
            and not any(
                req.get("details", {}).get("usage_provisional")
                for req in self.requests.values()
            ),
            "reserved_tokens": sum(self._pending.values()),
            "stage_remaining": {
                stage: {
                    "calls": max(
                        0, caps["calls"] - self.stages.get(stage, {}).get("calls", 0)
                    ),
                    "tokens": max(
                        0,
                        caps["tokens"]
                        - self.stages.get(stage, {}).get("known_actual_tokens", 0)
                        - self.stages.get(stage, {}).get("unknown_reserved_tokens", 0)
                        - sum(
                            self.requests[key].get("stage_reserved", tokens)
                            for key, tokens in self._pending.items()
                            if self.requests[key]["stage"] == stage
                        ),
                    ),
                }
                for stage, caps in self.stage_limits.items()
            },
            "cost_complete": not self.unknown_requests
            and not self._pending
            and all(
                r.get("released") or "estimated_cost" in r
                for r in self.requests.values()
            )
            and bool(self.requests),
        }

    async def _save(self):
        if self._persistence_failed:
            raise RuntimeError(
                "Budget persistence failed; reload the durable ledger before retrying"
            )
        self.revision += 1
        if self._persist:
            pending_write = asyncio.create_task(self._persist(self.snapshot()))
            try:
                await asyncio.shield(pending_write)
            except asyncio.CancelledError:
                # The transaction owns its connection until commit/rollback completes.
                # Do not abandon a billed settlement or cancel a driver during close.
                try:
                    await pending_write
                except BaseException:
                    self._persistence_failed = True
                raise
            except BaseException:
                self._persistence_failed = True
                raise

    def _stage(self, stage):
        return self.stages.setdefault(
            stage, {"calls": 0, "known_actual_tokens": 0, "unknown_reserved_tokens": 0}
        )

    async def reserve(
        self,
        tokens,
        *,
        stage="supervisor",
        branch=None,
        global_bound=None,
        operation_id=None,
    ):
        if tokens <= 0:
            raise ValueError("Reservation must be positive")
        async with self._lock:
            if self._persistence_failed:
                raise RuntimeError(
                    "Budget persistence failed; reload the durable ledger before retrying"
                )
            if self.stage_limits and stage not in self.stage_limits:
                raise ValueError("Unknown budget stage")
            retry_of = None
            legacy = [
                (key, request)
                for key, request in self.requests.items()
                if (request.get("recovery") or request.get("retry_authorization"))
                and request.get("sent")
                and not request.get("operation_id")
                and not request.get("retry_authorization", {}).get("consumed_by")
            ]
            if legacy:
                retry_of, prior_legacy = legacy[0]
                if (
                    not prior_legacy.get("retry_authorization")
                    or prior_legacy["stage"] != stage
                ):
                    raise ValueError(
                        "Legacy request recovery requires explicit retry authorization: "
                        + retry_of
                    )
            if operation_id:
                prior = [
                    (key, request)
                    for key, request in self.requests.items()
                    if request.get("operation_id") == operation_id
                    and not request.get("released")
                ]
                if prior:
                    retry_of, last = prior[-1]
                    authorization = last.get("retry_authorization", {})
                    if (
                        "usage" not in last
                        or not authorization
                        or authorization.get("consumed_by")
                    ):
                        raise ValueError(
                            "Request recovery requires explicit retry authorization: "
                            + retry_of
                        )
            global_reservation = max(tokens, global_bound or 0)
            if (
                self.charged_tokens + sum(self._pending.values()) + global_reservation
                > self.limit
            ):
                raise BudgetExceeded("Authorized token budget exhausted")
            if self.call_limit is not None and self.calls >= self.call_limit:
                raise BudgetExceeded("Authorized model call budget exhausted")
            row = self._stage(stage)
            caps = self.stage_limits.get(stage, {})
            pending = sum(
                self.requests[k].get("stage_reserved", v)
                for k, v in self._pending.items()
                if self.requests[k]["stage"] == stage
            )
            if row["calls"] >= caps.get("calls", float("inf")) or (
                row["known_actual_tokens"]
                + row["unknown_reserved_tokens"]
                + pending
                + tokens
                > caps.get("tokens", float("inf"))
            ):
                raise BudgetExceeded(
                    "Stage budget exhausted: " + stage,
                    details={
                        "stage": stage,
                        "requested_reservation": tokens,
                        "known_actual_tokens": row["known_actual_tokens"],
                        "unknown_reserved_tokens": row["unknown_reserved_tokens"],
                        "pending_reserved_tokens": pending,
                        "stage_remaining_tokens": max(
                            0,
                            caps.get("tokens", self.limit)
                            - row["known_actual_tokens"]
                            - row["unknown_reserved_tokens"]
                            - pending,
                        ),
                        "stage_calls": row["calls"],
                        "stage_call_limit": caps.get("calls"),
                    },
                )
            if branch and branch in caps.get("branches", {}):
                count = sum(
                    r.get("branch") == branch
                    and r["stage"] == stage
                    and not r.get("released")
                    for r in self.requests.values()
                )
                if count >= caps["branches"][branch]:
                    raise BudgetExceeded(
                        "Review branch call budget exhausted: " + branch
                    )
            if unresolved := self._restored_pending.intersection(self._pending):
                raise ValueError(
                    "Pending request recovery required before another model call: "
                    + ", ".join(sorted(unresolved))
                )
            key = uuid4().hex
            self._pending[key] = global_reservation
            self.requests[key] = {
                "stage": stage,
                "branch": branch,
                "reserved": global_reservation,
                "stage_reserved": tokens,
                "sent": False,
            }
            if operation_id:
                self.requests[key]["operation_id"] = operation_id
            if retry_of:
                self.requests[key]["retry_of"] = retry_of
                self.requests[retry_of]["retry_authorization"]["consumed_by"] = key
            self.calls += 1
            row["calls"] += 1
            await self._save()
            return key

    async def recover_pending(self, key, *, actor_id, note, usage=None, details=None):
        """Operator-audited recovery; a sent request is charged, never refunded."""
        if not str(actor_id).strip() or not str(note).strip():
            raise ValueError("Recovery requires actor and note")
        if usage is not None and (type(usage) is not int or usage < 0):
            raise ValueError("Invalid usage")
        audit = {
            "actor_id": actor_id,
            "note": note,
            "usage": usage,
            "details": deepcopy(details or {}),
        }
        async with self._lock:
            if self._persistence_failed:
                raise RuntimeError(
                    "Budget persistence failed; reload the durable ledger before retrying"
                )
            request = self.requests[key]
            if request.get("recovery"):
                if request["recovery"] == audit:
                    return
                raise ValueError("Conflicting pending recovery")
            if key not in self._pending:
                raise ValueError("Request is not pending")
            if not request.get("sent") and usage is not None:
                raise ValueError("Unsent requests cannot have billed usage")
            reserved = self._pending.pop(key)
            request["recovery"] = audit
            stage = self._stage(request["stage"])
            if not request.get("sent"):
                request["released"] = True
                self.calls -= 1
                stage["calls"] -= 1
            else:
                request["usage"] = usage
                request["details"] = deepcopy(details or {})
                if usage is None:
                    self.unknown_requests += 1
                    self.unknown_reserved_tokens += reserved
                    stage["unknown_reserved_tokens"] += reserved
                else:
                    self.known_actual_tokens += usage
                    stage["known_actual_tokens"] += usage
                self.charged_tokens += reserved if usage is None else usage
                self._price(request)
            await self._save()
            cap = self.stage_limits.get(request["stage"], {}).get(
                "tokens", float("inf")
            )
            if (
                self.charged_tokens + sum(self._pending.values()) > self.limit
                or stage["known_actual_tokens"] + stage["unknown_reserved_tokens"] > cap
            ):
                raise BudgetExceeded("Recovered usage exceeded the authorized budget")

    async def authorize_retry(self, key, *, actor_id, note):
        if not str(actor_id).strip() or not str(note).strip():
            raise ValueError("Retry authorization requires actor and note")
        async with self._lock:
            if self._persistence_failed:
                raise RuntimeError(
                    "Budget persistence failed; reload the durable ledger before retrying"
                )
            request = self.requests[key]
            if "usage" not in request or request.get("released"):
                raise ValueError("Recover pending requests before authorizing a retry")
            authorization = {"actor_id": actor_id, "note": note}
            if previous := request.get("retry_authorization"):
                if previous == authorization:
                    return
                raise ValueError("Conflicting or consumed retry authorization")
            request["retry_authorization"] = authorization
            await self._save()

    async def mark_sent(self, key):
        async with self._lock:
            if self._persistence_failed:
                raise RuntimeError(
                    "Budget persistence failed; reload the durable ledger before retrying"
                )
            if key not in self._pending:
                raise ValueError("Request is not pending")
            self.requests[key]["sent"] = True
            await self._save()

    async def release_unsent(self, key):
        async with self._lock:
            if self._persistence_failed:
                raise RuntimeError(
                    "Budget persistence failed; reload the durable ledger before retrying"
                )
            row = self.requests[key]
            if row.get("sent"):
                raise ValueError("Cannot release a sent request")
            if row.get("released"):
                return
            if key not in self._pending:
                raise ValueError("Request already settled")
            self._pending.pop(key)
            row["released"] = True
            self.calls -= 1
            self._stage(row["stage"])["calls"] -= 1
            await self._save()

    async def settle(self, reservation, usage, *, details=None):
        if usage is not None and (not isinstance(usage, int) or usage < 0):
            raise ValueError("Invalid usage")
        async with self._lock:
            if self._persistence_failed:
                raise RuntimeError(
                    "Budget persistence failed; reload the durable ledger before retrying"
                )
            request = self.requests[reservation]
            if "usage" in request:
                if request["usage"] != usage:
                    raise ValueError("Conflicting settlement")
                return
            reserved = self._pending.pop(reservation)
            request["usage"] = usage
            request["details"] = details or {}
            row = self._stage(request["stage"])
            if usage is None:
                self.unknown_requests += 1
                self.unknown_reserved_tokens += reserved
                row["unknown_reserved_tokens"] += reserved
            else:
                self.known_actual_tokens += usage
                row["known_actual_tokens"] += usage
            self.charged_tokens += reserved if usage is None else usage
            self._price(request)
            await self._save()
            caps = self.stage_limits.get(request["stage"], {})
            if self.charged_tokens + sum(self._pending.values()) > self.limit or (
                row["known_actual_tokens"] + row["unknown_reserved_tokens"]
                > caps.get("tokens", float("inf"))
            ):
                raise BudgetExceeded("Actual usage exceeded the authorized budget")

    async def reconcile(self, reservation, usage, *, details=None):
        """按 provider 账单校正已结算请求：未知占额或暂定用量 → 实际值。

        未知结算退回预留值占额、改按实际值计入已知；暂定结算按差额调整。
        只允许校正一次：已对账请求重复提交相同值幂等返回，不同值视为冲突。
        """
        if type(usage) is not int or usage < 0:
            raise ValueError("Invalid usage")
        async with self._lock:
            if self._persistence_failed:
                raise RuntimeError(
                    "Budget persistence failed; reload the durable ledger before retrying"
                )
            request = self.requests[reservation]
            if "usage" not in request:
                raise ValueError("Request is not settled")
            previous = request["usage"]
            if request.get("reconciled"):
                if previous == usage:
                    return
                raise ValueError("Conflicting reconciliation")
            verified_details = deepcopy(request.get("details", {}))
            breakdown = (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "reasoning_tokens",
            )
            if previous != usage:
                for name in breakdown:
                    verified_details.pop(name, None)
            for name in breakdown:
                if details and name in details:
                    verified_details[name] = details[name]
            counts = [
                verified_details.get(name) for name in ("input_tokens", "output_tokens")
            ]
            if any(value is not None for value in counts):
                cached = verified_details.get("cache_read_tokens", 0)
                if (
                    not all(type(value) is int and value >= 0 for value in counts)
                    or sum(counts) != usage
                    or type(cached) is not int
                    or not 0 <= cached <= counts[0]
                ):
                    raise ValueError(
                        "Reconciliation token breakdown does not match usage"
                    )
            request["reconciliation"] = {
                "previous_usage": previous,
                "previous_details": deepcopy(request.get("details", {})),
                "previous_estimated_cost": request.get("estimated_cost"),
                "usage": usage,
            }
            row = self._stage(request["stage"])
            if previous is None:
                reserved = request["reserved"]
                self.unknown_requests -= 1
                self.unknown_reserved_tokens -= reserved
                row["unknown_reserved_tokens"] -= reserved
                delta = usage - reserved
                self.known_actual_tokens += usage
                row["known_actual_tokens"] += usage
            else:
                delta = usage - previous
                self.known_actual_tokens += delta
                row["known_actual_tokens"] += delta
            self.charged_tokens += delta
            request["usage"] = usage
            request["reconciled"] = True
            request["details"] = {**verified_details, "usage_provisional": False}
            if details:
                request["reconcile_details"] = deepcopy(details)
            if "estimated_cost" in request:
                self.estimated_cost = str(
                    Decimal(self.estimated_cost or "0")
                    - Decimal(request.pop("estimated_cost"))
                )
                request.pop("price_version", None)
            self._price(request)
            await self._save()
            caps = self.stage_limits.get(request["stage"], {})
            if self.charged_tokens + sum(self._pending.values()) > self.limit or (
                row["known_actual_tokens"] + row["unknown_reserved_tokens"]
                > caps.get("tokens", float("inf"))
            ):
                raise BudgetExceeded("Reconciled usage exceeded the authorized budget")

    def _price(self, request):
        details = request["details"]
        rates = self.prices.get(details.get("model"))
        if request["usage"] is None or not rates or not rates.get("version"):
            return
        input_tokens, output = details.get("input_tokens"), details.get("output_tokens")
        cached = details.get("cache_read_tokens", 0)
        if input_tokens is None or output is None or not 0 <= cached <= input_tokens:
            return
        amount = (
            (input_tokens - cached) * Decimal(rates["input"])
            + cached * Decimal(rates["cache_read"])
            + output * Decimal(rates["output"])
        ) / Decimal(1_000_000)
        # 对账重计价时先冲销旧值，避免成本重复累加
        if "estimated_cost" in request:
            self.estimated_cost = str(
                Decimal(self.estimated_cost or "0") - Decimal(request["estimated_cost"])
            )
        request["estimated_cost"] = str(amount)
        request["price_version"] = rates["version"]
        self.estimated_cost = str(Decimal(self.estimated_cost or "0") + amount)
