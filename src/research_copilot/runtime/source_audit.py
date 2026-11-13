"""Source diagnostics: allowlisted metadata only, correlated with the host run."""

import json
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from uuid import uuid4

from research_copilot.runtime.control import active_dispatch

_audit = ContextVar("source_audit", default=None)
logger = logging.getLogger(__name__)


@contextmanager
def audit_context(context, export):
    token = _audit.set((context, export))
    try:
        yield
    finally:
        _audit.reset(token)


@asynccontextmanager
async def span(category, name, *, source):
    started = time.monotonic()
    scope = active_dispatch.get()
    binding = _audit.get()
    row = {
        "span_id": uuid4().hex,
        "category": category,
        "name": name,
        "source": source,
        "status": "completed",
        "dispatch_id": scope.dispatch_id if scope else None,
    }
    if binding:
        context, _ = binding
        row.update(
            report_id=context.report_id,
            run_id=context.run_id,
            budget_id=context.budget_id,
        )
    try:
        yield row
    except BaseException as exc:
        row.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        row["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        # Even an adapter accidentally adding input/output cannot export them.
        safe = {
            key: value
            for key, value in row.items()
            if key
            in {
                "span_id",
                "category",
                "name",
                "source",
                "status",
                "dispatch_id",
                "report_id",
                "run_id",
                "budget_id",
                "duration_ms",
                "error_type",
                "provider_request_id",
            }
        }
        logger.info("source_span %s", json.dumps(safe, ensure_ascii=False))
        if binding:
            await binding[1](safe)
