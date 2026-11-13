"""Frozen cross-case boundary replay. It never calls a model or a source.

Synthetic token traces exercise the ledger and budget curves; they are not
provider measurements or a claim about end-to-end research quality.
"""

import hashlib
import json
from pathlib import Path

from research_copilot.runtime.budget import BudgetExceeded, TokenBudget
from research_copilot.subagents.claim_validation import validate_claims
from research_copilot.sandbox.contracts import validate_option


async def _trace(case, limit):
    ledger = TokenBudget(limit=limit, budget_id="offline:" + case["id"])
    complete = True
    for row in case["trace"]:
        try:
            request = await ledger.reserve(row["reserved"])
            await ledger.mark_sent(request)
            await ledger.settle(
                request, row["input_tokens"] + row["output_tokens"], details=row
            )
        except BudgetExceeded:
            complete = False
            break
    return complete, ledger.snapshot()


async def evaluate_suite(root: Path):
    provenance = json.loads((root / "provenance.json").read_text("utf-8"))
    if provenance["usage_origin"] != "synthetic_offline":
        raise ValueError("This suite requires separately labelled synthetic traces")
    raw = (root / "cases.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != provenance["cases_sha256"]:
        raise ValueError("Frozen baseline fixture hash mismatch")
    cases = json.loads(raw)
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate evaluation case identity")
    output, calls, tokens = [], 0, 0
    for case in cases:
        errors = []
        if case["kind"] == "chart":
            try:
                validate_option(case["option"])
            except ValueError as exc:
                errors = [str(exc)]
        elif case["kind"] == "numeric":
            body = case["body"]
            claim = {"start": 0, "end": len(body), "quote": body, **case["references"]}
            errors = validate_claims(
                body,
                {
                    "body_hash": hashlib.sha256(body.encode()).hexdigest(),
                    "claims": [claim],
                },
                case["metrics"],
                case["evidence"],
                [],
                facts=case["facts"],
            )
        else:
            raise ValueError("Unknown evaluation kind")
        complete, ledger = await _trace(case, 10000)
        calls += ledger["calls"]
        tokens += ledger["known_actual_tokens"]
        output.append(
            {
                "id": case["id"],
                "scenario": case["scenario"],
                "expected_accept": case["expected_accept"],
                "accepted": not errors,
                "contract_correct": (not errors) == case["expected_accept"],
                "errors": errors,
                "trace_complete": complete,
                "replayed_calls": ledger["calls"],
                "replayed_tokens": ledger["known_actual_tokens"],
            }
        )
    curves = []
    for limit in (40, 100, 500):
        rows = [await _trace(case, limit) for case in cases]
        curves.append(
            {
                "token_limit_per_case": limit,
                "completed_replays": sum(done for done, _ in rows),
                "replayed_calls": sum(ledger["calls"] for _, ledger in rows),
                "replayed_tokens": sum(
                    ledger["known_actual_tokens"] for _, ledger in rows
                ),
            }
        )
    return {
        "format": 1,
        "fixture_sha256": provenance["cases_sha256"],
        "scope": "frozen_boundary_replay",
        "usage_origin": "synthetic_offline",
        "real_model_cost": None,
        "agent_completion_rate": None,
        "case_count": len(output),
        "contract_accuracy": sum(row["contract_correct"] for row in output)
        / len(output),
        "accepted_output_rate": sum(row["accepted"] for row in output) / len(output),
        "replayed_calls": calls,
        "replayed_tokens": tokens,
        "cases": output,
        "budget_curve": curves,
    }
