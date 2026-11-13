"""Real Docker checks: no paid models and no external data source."""

import asyncio
import json
import pytest
from research_copilot.runtime.control import (
    active_dispatch,
    RunControl,
    RunCancelled,
)
from tests.integration.test_searcher import dispatch
from tests.integration.test_analysist import financial_evidence
from tests.unit.test_orchestration import context

pytestmark = pytest.mark.docker


def service(env, **limits):
    from research_copilot.sandbox.config import SandboxConfig
    from research_copilot.sandbox.receipts import ExecutionService

    return ExecutionService(
        env[0], config=SandboxConfig(**limits), control=RunControl(), budget=None
    )


@pytest.mark.asyncio
async def test_real_docker_isolation_and_cleanup(role_env):
    await financial_evidence(role_env)
    scope = await dispatch(role_env, "analysist", "sandbox-isolation")
    token = active_dispatch.set(scope)
    try:
        executor = service(role_env)
        import shutil
        import py_compile

        compiled = role_env[0].report_root(context()) / "installed-resources"
        shutil.copytree(executor.resources_root, compiled)
        py_compile.compile(
            str(compiled / "skills/analysist/financial-modeling/scripts/growth.py"),
            doraise=True,
        )
        executor.resources_root = compiled
        root = role_env[0].report_root(context()) / "tasks" / scope.dispatch_id
        root.mkdir(parents=True, exist_ok=True)
        code = """import os, socket
from pathlib import Path
assert os.getuid() != 0
assert not list(Path('/skills').rglob('*.pyc'))
for path in ['/var/run/docker.sock','/proc/1/root/.env','/workspace/tasks/other-dispatch','/workspace/supervisor']:
    assert not Path(path).exists(), path
assert not any('TOKEN' in k or 'SECRET' in k or 'DATABASE' in k for k in os.environ)
try:
    socket.create_connection(('1.1.1.1',443),timeout=.2)
    raise AssertionError('network available')
except OSError: pass
for path in ['/skills/analysist/financial-modeling/SKILL.md','/workspace/evidence/sandbox-isolation/E-000001.json', '/workspace/tasks/sandbox-isolation/run.py']:
    try:
        with open(path,'a') as f: f.write('corrupt')
        raise AssertionError('input writable')
    except OSError: pass
print('isolation-ok')
"""
        (root / "run.py").write_text(code)
        receipt = await executor.execute(context(), "isolation", "python run.py")
        assert receipt["status"] == "completed", receipt
        assert "isolation-ok" in receipt["output"]
        assert receipt["metric_ids"] == []
        assert (
            await executor.execute(context(), "isolation", "python run.py") == receipt
        )
        with pytest.raises(ValueError, match="identity"):
            await executor.execute(context(), "isolation", "echo changed")
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_real_docker_receipts_registration_and_tamper(role_env):
    await financial_evidence(role_env)
    scope = await dispatch(role_env, "analysist", "sandbox-growth")
    token = active_dispatch.set(scope)
    try:
        executor = service(role_env)
        services = role_env[0]
        state = await role_env[2].read_control(context())
        root = services.report_root(context()) / "tasks" / scope.dispatch_id
        root.mkdir(parents=True, exist_ok=True)
        refs = []
        for i, (period, amount) in enumerate([("2024", "100"), ("2025", "120")], 1):
            ident = f"E-{i:06d}"
            refs.append(
                {
                    "key": f"revenue{i}",
                    "evidence_id": ident,
                    "content_hash": state.role_data["evidence"][ident]["content_hash"],
                    "pointer": "/revenue",
                    "raw_value": amount,
                    "unit": "CNY",
                    "currency": "CNY",
                    "period": period,
                    "entity": "AAA",
                    "basis": "consolidated",
                }
            )
        (root / "input.json").write_text(json.dumps(refs))
        (root / "run.py").write_text("""import json, os
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from io_contract import write_result
refs=json.loads(Path('input.json').read_text())
result=((Decimal(refs[1]['raw_value'])/Decimal(refs[0]['raw_value']))-1)*100
write_result(Path(os.environ['RC_OUTPUT_DIR'])/'result.json',metrics=[{'key':'growth','value':str(result.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)),'unit':'%','input_keys':['revenue2','revenue1'],'semantic_type':'growth','formula_id':'growth_percent','numerator_basis':'revenue 2025','denominator_basis':'revenue 2024','display_precision':2,'is_estimate':False}],input_refs=refs)
""")
        receipt = await executor.execute(context(), "growth", "python run.py")
        assert receipt["status"] == "registered", json.dumps(
            receipt, ensure_ascii=False
        )
        state = await role_env[2].read_control(context())
        metric = state.role_data["metrics"][scope.dispatch_id][receipt["metric_ids"][0]]
        assert (
            metric["value"] == "20.00"
            and metric["execution_id"] == receipt["execution_id"]
        )
        # Lineage order follows input_keys order; the set is what matters.
        assert sorted(metric["evidence_ids"]) == ["E-000001", "E-000002"]
        from research_copilot.subagents.analysist import AnalysisService

        candidate = {
            "summary": "Growth",
            "conclusions": ["20 percent"],
            "evidence_ids": metric["evidence_ids"],
            "metric_ids": receipt["metric_ids"],
            "chart_ids": [],
            "assumptions": [],
            "limitations": [],
            "claims": [
                {
                    "claim_id": "growth",
                    "text": "20 percent",
                    "metric_ids": receipt["metric_ids"],
                    "evidence_ids": metric["evidence_ids"],
                }
            ],
        }
        (root / "candidate.json").write_text(json.dumps(candidate))
        saved = await AnalysisService(services).save_candidate(
            context(), "save", scope.write_prefix + "candidate.json"
        )
        assert saved["analysis_id"]
        execution = state.role_data["executions"][receipt["execution_id"]]
        code = services.report_root(context()) / execution["code_paths"][0]
        code.write_text("tampered")
        with pytest.raises(ValueError, match="hash"):
            await AnalysisService(services).save_candidate(
                context(), "save-again", scope.write_prefix + "candidate.json"
            )
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_real_docker_timeout_and_log_flood(role_env):
    """Timeout records need explicit diagnosis, so the timeout phase runs last."""
    executor = service(role_env, timeout_seconds=2)

    scope = await dispatch(role_env, "analysist", "sandbox-limits-logs")
    token = active_dispatch.set(scope)
    try:
        out = await executor.execute(
            context(), "logs", "python -c 'print(\"X\"*1000000)'"
        )
        assert out["truncated"] and len(out["output"].encode()) <= 65536
    finally:
        active_dispatch.reset(token)

    scope = await dispatch(role_env, "analysist", "sandbox-limits-timeout")
    token = active_dispatch.set(scope)
    try:
        out = await executor.execute(
            context(),
            "timeout",
            "python -u -c 'import time;print(\"before-timeout\",flush=True);time.sleep(10)'",
        )
        assert out["status"] == "timeout" and not out["metric_ids"]
        assert "before-timeout" in out["output"]
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_real_docker_cancel_cleans_containers(role_env):
    scope = await dispatch(role_env, "analysist", "sandbox-limits-cancel")
    token = active_dispatch.set(scope)
    try:
        executor = service(role_env)
        task = asyncio.create_task(
            executor.execute(
                context(), "cancel", "python -c 'import time;time.sleep(20)'"
            )
        )
        await asyncio.sleep(0.5)
        executor.control.cancel()
        with pytest.raises(RunCancelled):
            await task
        import subprocess

        listed = await asyncio.to_thread(
            subprocess.run,
            ["docker", "ps", "-aq", "--filter", "label=rc.report=report-1"],
            capture_output=True,
            check=True,
        )
        assert not listed.stdout.strip()
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_registration_recovers_immutable_output_without_reexecution(
    role_env, monkeypatch
):
    scope = await dispatch(role_env, "analysist", "sandbox-recovery")
    token = active_dispatch.set(scope)
    try:
        executor = service(role_env)
        store = role_env[2]
        mutate = store.mutate_control

        async def fail_publish(context, operation):
            if operation.__name__ == "publish":
                raise RuntimeError("simulated storage outage after output freeze")
            return await mutate(context, operation)

        monkeypatch.setattr(store, "mutate_control", fail_publish)
        with pytest.raises(RuntimeError, match="storage outage"):
            await executor.execute(
                context(),
                "recover",
                'python -c \'import os,uuid;from pathlib import Path;Path(os.environ["RC_OUTPUT_DIR"],"nonce.txt").write_text(str(uuid.uuid4()))\'',
            )
        monkeypatch.setattr(store, "mutate_control", mutate)
        state = await store.read_control(context())
        record = next(iter(state.role_data["executions"].values()))
        assert record["status"] == "output_ready"
        root = role_env[0].report_root(context()) / record["root"]
        original = (root / "output/nonce.txt").read_text()

        # Disallow all external execution on recovery; registration must use the archive.
        async def no_execute(**kwargs):
            raise AssertionError("reexecuted Docker during registration recovery")

        monkeypatch.setattr(executor.runner, "run", no_execute)
        out = await executor.execute(context(), "recover", record["command"])
        assert (
            out["status"] == "completed"
            and (root / "output/nonce.txt").read_text() == original
        )
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_unknown_execution_never_replays(role_env, monkeypatch):
    from research_copilot.sandbox.runner import ExecutionUnknown

    scope = await dispatch(role_env, "analysist", "sandbox-unknown")
    token = active_dispatch.set(scope)
    try:
        executor = service(role_env)
        original = executor.runner._command

        async def lose_inspect(*args, **kwargs):
            if args[0] == "inspect":
                return 1, b"", b"lost outcome", False
            return await original(*args, **kwargs)

        monkeypatch.setattr(executor.runner, "_command", lose_inspect)
        with pytest.raises(ExecutionUnknown):
            await executor.execute(context(), "unknown", "echo ran-once")
        monkeypatch.setattr(executor.runner, "_command", original)
        with pytest.raises(ExecutionUnknown, match="automatic replay"):
            await executor.execute(context(), "unknown", "echo ran-once")
        with pytest.raises(ExecutionUnknown, match="unknown or unregistered execution"):
            await executor.execute(context(), "new-identity", "echo ran-once")
        record = next(
            iter(
                (await role_env[2].read_control(context()))
                .role_data["executions"]
                .values()
            )
        )
        assert record["status"] == "unknown" and not record["response"]["metric_ids"]
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,expected",
    [
        (
            'python -c \'from pathlib import Path;import os;Path(os.environ["RC_OUTPUT_DIR"],"bad").symlink_to("/etc/passwd")\'',
            "invalid",
        ),
        (
            'python -c \'import os;from pathlib import Path;Path(os.environ["RC_OUTPUT_DIR"],"result.json").write_text("{}")\'',
            "invalid_result",
        ),
        (
            "python -c 'import os;from pathlib import Path;[Path(os.environ[\"RC_OUTPUT_DIR\"],str(i)).touch() for i in range(101)]'",
            "invalid",
        ),
    ],
)
async def test_real_docker_rejects_linked_malformed_and_excess_outputs(
    role_env, command, expected
):
    scope = await dispatch(role_env, "analysist", "sandbox-output")
    token = active_dispatch.set(scope)
    try:
        executor = service(role_env)
        if expected == "invalid":
            with pytest.raises(ValueError):
                await executor.execute(context(), "output", command)
        else:
            result = await executor.execute(context(), "output", command)
            assert result["status"] == expected and not result["metric_ids"]
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_real_docker_oom_and_descendant_cleanup(role_env):
    scope = await dispatch(role_env, "analysist", "sandbox-oom")
    token = active_dispatch.set(scope)
    try:
        executor = service(role_env, memory_mib=128)
        out = await executor.execute(
            context(), "oom", "python -c 'x=bytearray(512*1024*1024)'"
        )
        assert (
            out["status"] in {"oom", "failed"}
            and out["exit_code"] != 0
            and not out["metric_ids"]
        )
        out = await executor.execute(
            context(),
            "child",
            'python -c \'import subprocess;subprocess.Popen(["sleep","1000"],start_new_session=True);print("spawned")\'',
        )
        assert out["status"] == "completed" and "spawned" in out["output"]
        import subprocess

        listed = await asyncio.to_thread(
            subprocess.run,
            ["docker", "ps", "-aq", "--filter", "label=rc.report=report-1"],
            capture_output=True,
            check=True,
        )
        assert not listed.stdout.strip()
    finally:
        active_dispatch.reset(token)


@pytest.mark.asyncio
async def test_restart_reclaims_dead_owner_only(role_env):
    import socket
    import os
    from uuid import uuid4
    from research_copilot.sandbox.runner import LABEL

    executor = service(role_env)
    runner = executor.runner
    dead = "rc-orphan-" + uuid4().hex
    live = "rc-active-" + uuid4().hex
    try:
        for name, pid in [(dead, 2147483647), (live, os.getpid())]:
            code, _, error, _ = await runner._command(
                "run",
                "-d",
                "--pull",
                "never",
                "--name",
                name,
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--label",
                LABEL + "=1",
                "--label",
                f"rc.owner={socket.gethostname()}:{pid}",
                executor.config.image,
                "sleep",
                "1000",
            )
            assert code == 0, error
        removed = await runner.cleanup_orphans()
        assert removed
        code, _, _, _ = await runner._command("inspect", dead)
        assert code != 0
        code, _, _, _ = await runner._command("inspect", live)
        assert code == 0
    finally:
        await runner._command("rm", "-f", dead, live)


@pytest.mark.asyncio
async def test_real_docker_run_metric_specs_registers_declared_metrics(role_env):
    from research_copilot.subagents.analysist import AnalysisService
    from research_copilot.subagents.contracts import AssumptionSpec, MetricSpec

    await financial_evidence(role_env)
    services = role_env[0]
    scope = await dispatch(role_env, "analysist", "spec-dispatch")
    state = await role_env[2].read_control(context())

    def fact_ref(evidence_id, record, key):
        raw = record["raw"]
        return {
            "key": key,
            "evidence_id": evidence_id,
            "content_hash": record["content_hash"],
            "pointer": "/revenue",
            "raw_value": raw["revenue"],
            "unit": raw["context"]["unit"],
            "currency": raw["context"]["unit"],
            "period": raw["context"]["period"],
            "entity": raw["context"]["entity"],
            "basis": raw["context"]["basis"],
            "scale": raw["context"]["scale"],
            "transformation": "identity",
        }

    def seed(state):
        evidence = state.role_data["evidence"]
        state.role_data.setdefault("fact_sheets", {})["facts-spec"] = {
            "facts": [
                {
                    "input_ref": fact_ref(
                        "E-000001", evidence["E-000001"], "revenue_2024"
                    )
                },
                {
                    "input_ref": fact_ref(
                        "E-000002", evidence["E-000002"], "revenue_2025"
                    )
                },
            ],
            "evidence_ids": ["E-000001", "E-000002"],
            "dispatch_id": scope.dispatch_id,
            "plan": None,
            "missing_required": [],
            "missing_optional": [],
            "gaps": [],
        }
        sheets = state.role_data["scopes"][scope.dispatch_id].setdefault(
            "fact_sheet_ids", []
        )
        if "facts-spec" not in sheets:
            sheets.append("facts-spec")

    await role_env[2].mutate_control(context(), seed)
    executor = service(role_env)
    services.sandbox_execution = executor
    token = active_dispatch.set(scope)
    try:
        output = await AnalysisService(services).run_metric_specs(
            context(),
            "spec-call-1",
            [
                MetricSpec(
                    key="revenue_growth",
                    semantic_type="growth",
                    formula_id="growth_percent",
                    numerator_basis="revenue 2025",
                    denominator_basis="revenue 2024",
                    unit="%",
                    input_keys=["revenue_2025", "revenue_2024"],
                    display_precision=2,
                    is_estimate=False,
                    description="Revenue growth",
                )
            ],
            [
                AssumptionSpec(
                    key="tax_rate",
                    value="0.25",
                    basis="Statutory rate assumption",
                    impact="Only affects after-tax intermediate values",
                )
            ],
        )
    finally:
        active_dispatch.reset(token)
    assert output["ok"] is True, output
    assert output["status"] == "registered"
    assert set(output["metric_key_to_id"]) == {"revenue_growth"}
    state = await role_env[2].read_control(context())
    metric_id = output["metric_key_to_id"]["revenue_growth"]
    receipt = state.role_data["metrics"][scope.dispatch_id][metric_id]
    assert receipt["formula_version"] == "sandbox-code-v1"
    assert receipt["execution_id"] in state.role_data["executions"]
    assert receipt["value"] == "20.00"
