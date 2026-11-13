"""Explicit loopback browser fixture: real PG/backend/graph; fake model responses.

Requires RC_TEST_DATABASE_URL with CREATE DATABASE rights in an isolated cluster.
Creates/drops only its own random rc_web_* database. Never reads .env or model config.
"""

import argparse
import json
from pathlib import Path
import os
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL
import uvicorn

from research_copilot.runtime.budget import TokenBudget
from research_copilot.runtime.business import BusinessCoordinator, ResearchPlan
from research_copilot.runtime.loop import run_async
from research_copilot.runtime.session import SessionRecord
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.runtime.urls import to_checkpoint_url
from research_copilot.subagents.services import RoleServices
from research_copilot.web.app import create_app
from tests.integration.conftest import plan_tasks
from tests.integration.test_web_review_flow import prepare_review_backend
from tests.unit.test_orchestration import context


async def main(args):
    root = args.workspace.resolve()
    root.mkdir(parents=True, exist_ok=False)
    base = to_checkpoint_url(os.environ["RC_TEST_DATABASE_URL"])
    params = psycopg.conninfo.conninfo_to_dict(base)
    name = "rc_web_" + uuid4().hex
    url = URL.create(
        "postgresql",
        username=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=int(params.get("port", 5432)),
        database=name,
    ).render_as_string(hide_password=False)
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            async with postgres_runtime(url, initialize=True) as (store, saver):
                if args.fresh:
                    from tests.integration.test_full_product_flow import full_backend

                    backend, _, _ = full_backend(store, saver, root / "workspace")
                else:
                    await store.create(
                        SessionRecord(
                            "report-1",
                            "actor-1",
                            "v2",
                            "policy-1",
                            "budget-1",
                            run_id="run-1",
                            user_request="离线联合验收夹具：预制草稿审核，不是真实研究",
                        ),
                        TokenBudget(limit=1000000, budget_id="budget-1").snapshot(),
                    )
                    business = BusinessCoordinator(store)
                    plan = await business.propose_plan(
                        context(),
                        ResearchPlan(
                            report_type="company_report",
                            objective="offline fixture",
                            tasks=plan_tasks(),
                        ),
                    )
                    await store.mutate_control(
                        context(), lambda state: state.approved.update(plan=plan)
                    )
                    services = RoleServices(
                        repository=store,
                        workspace_root=root / "workspace",
                        business=business,
                        report_rules={
                            "version": 1,
                            "sensitive_words": [],
                            "prohibited_patterns": [],
                            "required_disclosures": ["risk_warning", "disclaimer"],
                            "risk_warning_template": "Data and methodology risks apply.",
                            "disclaimer_template": "For research reference only.",
                            "is_example": False,
                        },
                    )
                    backend, _ = await prepare_review_backend(
                        (services, business, store), root, saver
                    )
                worker = None
                if args.run_recovery:
                    from research_copilot.runtime.run_queue import RunQueue
                    from tests.integration.test_run_queue import expire

                    queue = RunQueue(store.engine)
                    await queue.enqueue(
                        report_id="report-1",
                        actor_id="actor-1",
                        operation_id="offline-lost-command",
                        kind="resume",
                        payload={"decision": None},
                    )
                    claim = await queue.claim("offline-stopped-owner")
                    await queue.start(claim)
                    await expire(store, claim)
                    await queue.claim("offline-observer")
                    budget = await store.load_budget(context())
                    request_id = await budget.reserve(
                        100, operation_id="offline-lost-provider-call"
                    )
                    await budget.mark_sent(request_id)
                    (root / "recovery-fixture.json").write_text(
                        json.dumps(
                            {
                                "report_id": "report-1",
                                "job_id": claim["id"],
                                "request_id": request_id,
                                "calls_before": budget.calls,
                                "note": "Injected lost execution; no provider call",
                            }
                        ),
                        encoding="utf-8",
                    )
                if args.retrospective_recovery:
                    from research_copilot.runtime.experience import (
                        ExtractionResult,
                        RetrospectiveWorker,
                    )

                    class OfflineExtractor:
                        calls = 0

                        async def extract(self, materials):
                            self.calls += 1
                            if self.calls == 1:
                                raise RuntimeError(
                                    "Offline recovery fixture: simulated lost response, no provider call"
                                )
                            return ExtractionResult(
                                drafts=(),
                                usage={
                                    "input_tokens": 10,
                                    "output_tokens": 2,
                                    "total_tokens": 12,
                                },
                            )

                    worker = RetrospectiveWorker(
                        store, OfflineExtractor(), poll_interval=0.1
                    )
                    await worker.start()
                app = create_app(
                    backend=backend,
                    static_dir=Path(__file__).resolve().parents[2] / "frontend/dist",
                )
                if args.hold_queue:
                    original_start = backend.runs.worker.start

                    async def held_start():
                        return None

                    backend.runs.worker.start = held_start

                    @app.post("/__fixture__/start-queue")
                    async def start_queue():
                        backend.runs.worker.start = original_start
                        await backend.runs.start()
                        return {"started": True}

                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        host="127.0.0.1",
                        port=args.port,
                        log_level="warning",
                        lifespan="off",
                    )
                )

                @app.post("/__fixture__/stop")
                async def stop():
                    server.should_exit = True
                    return {"stopping": True}

                print(
                    f"Isolated fixture: http://127.0.0.1:{args.port}"
                    + ("/" if args.fresh else "/reports/report-1"),
                    flush=True,
                )
                try:
                    await server.serve()
                finally:
                    if worker is not None:
                        await worker.stop()
                    await backend.runs.aclose()
        finally:
            assert name.startswith("rc_web_") and name != params.get("dbname")
            await admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port", type=int, default=5192)
    parser.add_argument(
        "--hold-queue",
        action="store_true",
        help="Persist commands without consuming until the fixture start-queue endpoint is called",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Start empty; run frozen models/sources through the real Docker product workflow",
    )
    parser.add_argument(
        "--retrospective-recovery",
        action="store_true",
        help="Run a fake retrospective: first response lost, manually authorized retry succeeds",
    )
    parser.add_argument(
        "--run-recovery",
        action="store_true",
        help="Inject a stopped research command and sent pending request; no provider call",
    )
    run_async(main(parser.parse_args()))
