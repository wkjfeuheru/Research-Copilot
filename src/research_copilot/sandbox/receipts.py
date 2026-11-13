"""Freeze authorized inputs; journal executions before Docker; register outputs atomically."""

import asyncio
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from filelock import FileLock, Timeout
from pydantic import ValidationError
from research_copilot.runtime.budget import BudgetExceeded
from research_copilot.runtime.control import RunCancelled
from research_copilot.runtime.task_completion import await_completion
from research_copilot.subagents.services import canonical, digest, immutable_write
from research_copilot.workspace import (
    bundled_resources,
    validate_resources,
    resource_files,
)
from research_copilot.workspace.filesystem import reject_links, tree_files
from .config import SandboxConfig
from .contracts import (
    ResultManifest,
    ChartManifest,
    validate_option,
    validate_png,
    validate_svg,
    relative_path,
)
from .runner import DockerRunner, ExecutionUnknown, import_archive


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _manifest_error_hint(error):
    """把 pydantic 校验错误压缩成“字段路径（原因）”的可执行提示。"""
    parts = []
    for item in error.errors()[:4]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "root"
        parts.append(f"{location}（{item.get('msg', '不合法')}）")
    return "、".join(parts)


def verify_execution(root, record):
    if record.get("status") != "registered":
        raise ValueError("Execution is not registered")
    for path, expected in record["files"].items():
        if sha(reject_links(root / path).read_bytes()) != expected:
            raise ValueError("Execution artifact hash mismatch")
    return record


class SandboxQueueTimeout(TimeoutError):
    """Admission expired before an execution record or container was created."""


class ExecutionService:
    def __init__(
        self,
        services,
        *,
        config=None,
        control,
        budget,
        runner=None,
        resources_root=None,
        admission_check=None,
        slot_claims=None,
    ):
        self.services = services
        self.config = config or SandboxConfig.load()
        self.control, self.budget = control, budget
        self.runner = runner or DockerRunner(self.config)
        self.resources_root = resources_root or bundled_resources()
        self.admission_check = admission_check
        self.slot_claims = slot_claims
        self._startup_lock = asyncio.Lock()
        self._started = False

    @asynccontextmanager
    async def _locks(self, report_id, dispatch_id):
        # Cross-process locks, shared by factories/restarts on the same workspace.
        root = reject_links(self.services.workspace_root / ".sandbox-locks")
        root.mkdir(parents=True, exist_ok=True)
        dispatch = FileLock(root / (report_id + "-execution.lock"))

        deadline = time.monotonic() + self.config.queue_seconds

        async def check_queue():
            await self.control.acheck()
            if time.monotonic() >= deadline:
                raise SandboxQueueTimeout(
                    "sandbox_queue_timeout: 等待执行槽超时，尚未启动容器"
                )

        async def acquire(lock):
            while True:
                await check_queue()
                try:
                    lock.acquire(timeout=0)
                    return
                except Timeout:
                    await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))

        await acquire(dispatch)
        slot = None
        try:
            while slot is None:
                await check_queue()
                quarantined = 0
                for index in range(self.config.concurrency):
                    candidate = FileLock(root / f"slot-{index}.lock")
                    try:
                        candidate.acquire(timeout=0)
                        try:
                            if (
                                self.slot_claims is not None
                                and not await self.slot_claims.available(index)
                            ):
                                quarantined += 1
                                candidate.release()
                                continue
                        except BaseException:
                            candidate.release()
                            raise
                        slot = candidate
                        break
                    except Timeout:
                        pass
                if slot is None:
                    if quarantined == self.config.concurrency:
                        from research_copilot.runtime.sandbox_slots import (
                            SandboxSlotBusy,
                        )

                        raise SandboxSlotBusy(
                            "All sandbox slots retain unsettled external work; inspect rc sandbox-claims"
                        )
                    await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
            # A dead process releases file locks, but its external execution may
            # still be live. Managed hosts must reject unsettled runtime owners
            # AFTER acquiring the newly available slot, before touching Docker.
            if self.admission_check is not None:
                await self.admission_check()
            yield index
        finally:
            if slot:
                slot.release()
            dispatch.release()

    async def _startup(self):
        async with self._startup_lock:
            if not self._started:
                await self.runner.cleanup_orphans()
                self._started = True

    async def execute(self, context, tool_call_id, command, timeout=None):
        scope, state = await self.services.scope(context, "analysist")
        if state.role_data["scopes"][scope.dispatch_id].get("submission_repair"):
            from research_copilot.subagents.contracts import BusinessInputError

            raise BusinessInputError(
                "submission-only repair cannot execute; reuse registered metric receipts"
            )
        if (
            not isinstance(command, str)
            or not command.strip()
            or len(command) > 16000
            or "\x00" in command
        ):
            raise ValueError("A bounded nonempty command is required")
        if not tool_call_id:
            raise PermissionError("Trusted tool call identity required")
        async with self._locks(context.report_id, scope.dispatch_id) as slot:
            await self.control.acheck()
            scope, state = await self.services.scope(context, "analysist")
            ident = (
                "exec-"
                + digest([context.report_id, scope.dispatch_id, tool_call_id])[:32]
            )
            identity = digest([command, timeout])
            old = state.role_data.get("executions", {}).get(ident)
            if old:
                if old["identity"] != identity:
                    raise ValueError("Execution tool call identity changed")
                if old["status"] == "output_ready":
                    return await self._register(context, scope, ident, old)
                if old["status"] in {"started", "unknown"}:
                    raise ExecutionUnknown(
                        "Previous execution outcome is unknown; automatic replay is forbidden"
                    )
                if old["status"] == "registered":
                    verify_execution(self.services.report_root(context), old)
                return old["response"]
            from research_copilot.sandbox.execution_policy import (
                check_execution_policy,
                execution_window_start,
                work_key,
            )

            records = check_execution_policy(
                scope.dispatch_id,
                state.role_data.get("scopes", {}),
                state.dispatches,
                list(state.role_data.get("executions", {}).values()),
                self.config.executions_per_dispatch,
                extensions=state.role_data.get("execution_extensions", []),
            )
            if any(r["status"] in {"started", "unknown"} for r in records):
                raise ExecutionUnknown(
                    "Dispatch contains an unknown execution; a new tool identity cannot bypass it"
                )
            if len(records) >= self.config.executions_per_dispatch:
                raise BudgetExceeded(
                    "Sandbox execution count exhausted",
                    details={"code": "sandbox_execution_limit"},
                )
            now = time.time()
            window_start = execution_window_start(
                records,
                work_key(scope.dispatch_id, {}, state.dispatches),
                state.role_data.get("execution_extensions", []),
                now=now,
            )
            if records and now - window_start >= self.config.task_seconds:
                # The window is wall clock, so a host pause also consumes it. The
                # operator lever is extend-executions (it starts a fresh window).
                raise BudgetExceeded(
                    "Sandbox dispatch deadline exhausted",
                    details={
                        "code": "sandbox_deadline_exhausted",
                        "task_seconds": self.config.task_seconds,
                        "elapsed_seconds": int(now - window_start),
                    },
                )
            if self.budget:
                budget = self.budget.snapshot()
                if (
                    budget["charged_tokens"] + sum(budget["pending"].values())
                    >= budget["limit"]
                ):
                    raise BudgetExceeded(
                        "Task budget exhausted",
                        details={"code": "task_token_budget_exhausted"},
                    )
            claim = (
                self.slot_claims.hold(slot, context.report_id, scope.dispatch_id, ident)
                if self.slot_claims is not None
                else nullcontext()
            )
            async with claim as lease:
                validate_resources(self.resources_root)
                await self._startup()
                image = await self.runner.image_identity()
                old_image = state.role_data.get("sandbox_image")
                if old_image and old_image != image:
                    raise ValueError("Sandbox image changed during the report")
                root = self.services.report_root(context)
                relative = f"artifacts/{scope.dispatch_id}/executions/{ident}"
                work = reject_links(root / relative)
                snapshot = work / "snapshot"
                files = await await_completion(
                    asyncio.to_thread(self._freeze, context, scope, state, snapshot)
                )
                code_paths = [path for path in files if "/tasks/" in path]
                record = {
                    "execution_id": ident,
                    "report_id": context.report_id,
                    "dispatch_id": scope.dispatch_id,
                    "tool_call_id": tool_call_id,
                    "command": command,
                    "identity": identity,
                    "started_at": now,
                    "status": "started",
                    "image_digest": image,
                    "policy_version": context.policy_version,
                    "budget_id": context.budget_id,
                    "code_hash": digest({p: files[p] for p in code_paths}),
                    "input_hash": digest(files),
                    "skill_manifest_hash": sha(
                        (self.resources_root / "manifest.json").read_bytes()
                    ),
                    "files": files,
                    "code_paths": code_paths,
                    "root": relative,
                    "evidence_hashes": {
                        eid: state.role_data["evidence"][eid]["content_hash"]
                        for eid in state.role_data["scopes"][scope.dispatch_id][
                            "evidence_ids"
                        ]
                    },
                }

                def start(state):
                    from research_copilot.runtime.rework import validate_dispatch

                    validate_dispatch(state, state.dispatches[scope.dispatch_id])
                    check_execution_policy(
                        scope.dispatch_id,
                        state.role_data.get("scopes", {}),
                        state.dispatches,
                        list(state.role_data.get("executions", {}).values()),
                        self.config.executions_per_dispatch,
                        extensions=state.role_data.get("execution_extensions", []),
                    )
                    if state.role_data.setdefault("sandbox_image", image) != image:
                        raise ValueError("Sandbox image changed")
                    state.role_data.setdefault("executions", {})[ident] = record

                await self.services.repository.mutate_control(context, start)
                result = None
                try:
                    timeout_limit = self.config.effective_timeout(timeout)
                    if records:
                        timeout_limit = min(
                            timeout_limit,
                            max(
                                1,
                                int(self.config.task_seconds - (now - window_start)),
                            ),
                        )
                    if lease is not None:
                        lease.external_started = True
                    result = await self.runner.run(
                        execution_id=ident,
                        report_id=context.report_id,
                        dispatch_id=scope.dispatch_id,
                        snapshot=snapshot,
                        command=command,
                        control=self.control,
                        timeout=timeout_limit,
                        image_digest=image,
                    )
                    # A failed/abandoned Docker command can finish remotely later.
                    # An empty listing alone cannot settle that uncertainty. Only
                    # a normally returned runner is eligible for automatic release.
                    if lease is not None:

                        async def confirm():
                            try:
                                await self.runner.confirm_cleanup(ident)
                            except Exception as exc:
                                raise ExecutionUnknown(
                                    "Cannot confirm sandbox cleanup; capacity retained"
                                ) from exc
                            lease.external_stopped = True

                        await await_completion(confirm())
                    await self.control.acheck()
                except BaseException as exc:
                    record["output"] = getattr(
                        exc, "sandbox_output", result.output if result else ""
                    )
                    record["truncated"] = getattr(
                        exc, "sandbox_truncated", result.truncated if result else False
                    )
                    record["status"] = (
                        "cancelled"
                        if isinstance(exc, (RunCancelled, asyncio.CancelledError))
                        and (
                            lease is None
                            or not lease.external_started
                            or lease.external_stopped
                        )
                        else "unknown"
                    )
                    record["response"] = {
                        "execution_id": ident,
                        "status": record["status"],
                        "metric_ids": [],
                        "chart_ids": [],
                        "error": type(exc).__name__,
                        "output": record["output"],
                        "truncated": record["truncated"],
                    }
                    await asyncio.shield(self._save(context, ident, record))
                    raise
                record.update(
                    exit_code=result.exit_code,
                    output=result.output,
                    truncated=result.truncated,
                    seconds=result.seconds,
                )
                record["response"] = {
                    "execution_id": ident,
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "output": result.output,
                    "truncated": result.truncated,
                    "metric_ids": [],
                    "chart_ids": [],
                    "files": [],
                }
                if result.status != "completed":
                    record["status"] = result.status
                    await self._save(context, ident, record)
                    return record["response"]
                archive_path = work / "output.tar"
                immutable_write(archive_path, result.archive)
                record["files"][
                    str(archive_path.relative_to(root)).replace("\\", "/")
                ] = sha(result.archive)
                record["status"] = "output_ready"
                await self._save(context, ident, record)
                return await self._register(context, scope, ident, record)

    def _freeze(self, context, scope, state, snapshot):
        root = self.services.report_root(context)
        hashes = {}
        total = 0

        def copy(source, destination):
            nonlocal total
            raw = reject_links(source).read_bytes()
            total += len(raw)
            if len(hashes) >= 1500 or total > 128 * 1024 * 1024:
                raise ValueError("Execution input snapshot exceeds limit")
            immutable_write(destination, raw)
            hashes[destination.relative_to(root).as_posix()] = sha(raw)

        task = reject_links(root / scope.write_prefix.removeprefix("/workspace/"))
        task.mkdir(parents=True, exist_ok=True)
        (snapshot / "workspace/tasks" / scope.dispatch_id).mkdir(
            parents=True, exist_ok=True
        )
        for source in tree_files(task):
            copy(
                source,
                snapshot
                / "workspace/tasks"
                / scope.dispatch_id
                / source.relative_to(task),
            )
        for virtual, expected in state.role_data["scopes"][scope.dispatch_id][
            "paths"
        ].items():
            if "/snapshot/" in virtual or virtual.endswith("output.tar"):
                continue
            source = root / virtual.removeprefix("/workspace/")
            if sha(reject_links(source).read_bytes()) != expected:
                raise ValueError("Authorized input hash mismatch")
            copy(source, snapshot / virtual.lstrip("/"))
        skills = self.resources_root / "skills/analysist"
        for source in resource_files(self.resources_root):
            if source.is_relative_to(skills):
                copy(source, snapshot / "skills/analysist" / source.relative_to(skills))
                # Old generated code may still import the underscore path.
                legacy = (
                    source.relative_to(skills)
                    .as_posix()
                    .replace("financial-modeling/", "financial_modeling/", 1)
                )
                copy(source, snapshot / "skills/analysist" / legacy)
        return hashes

    async def _save(self, context, ident, record):
        await self.services.repository.mutate_control(
            context,
            lambda state: state.role_data.setdefault("executions", {}).__setitem__(
                ident, json.loads(canonical(record))
            ),
        )

    async def _register(self, context, scope, ident, record):
        root = self.services.report_root(context)
        for path, expected in record["files"].items():
            if sha(reject_links(root / path).read_bytes()) != expected:
                raise ValueError("Execution recovery hash mismatch")
        output = root / record["root"] / "output"
        archive = (root / record["root"] / "output.tar").read_bytes()
        # Recovery validates a fresh temporary extraction before reusing immutable files.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="rc-output-") as temporary:
            files = import_archive(archive, Path(temporary), self.config)
        try:
            _, state = await self.services.scope(context, "analysist")
            metrics, charts = {}, {}
            if "result.json" in files:
                try:
                    result = ResultManifest.model_validate_json(files["result.json"])
                except ValidationError as exc:
                    # 原始 pydantic 报错不易定位；指出字段与正确写法，便于一次修正
                    # （WLY-20261108 第十一轮 charts 写成对象导致两次无效执行）。
                    raise ValueError(
                        "result.json 结构不合法："
                        + _manifest_error_hint(exc)
                        + "；charts 必须是图表清单文件路径字符串数组（如 "
                        '["charts/core_perf.json"]），不能是对象。'
                    ) from exc
                if result.schema_version != 2:
                    raise ValueError(
                        "New executions require result schema_version=2; legacy receipts remain read-only"
                    )
                inputs = self._validate_inputs(result, state, scope, record)
                from research_copilot.subagents.metric_quality import (
                    validate_metric_batch,
                )

                operands = {
                    **inputs,
                    **{
                        k: {"assumption_key": k, "is_estimate": True, **v.model_dump()}
                        for k, v in result.assumptions.items()
                    },
                }
                brief = (
                    state.role_data["scopes"][scope.dispatch_id].get("task_brief") or {}
                )
                requirements = [
                    *state.role_data.get("regression_criteria", {}).get(
                        "required_metrics", []
                    ),
                    *[
                        {
                            "key": item.get("key"),
                            "semantic_type": item.get("semantic_type"),
                            "unit": item.get("unit"),
                        }
                        for item in brief.get("required_metrics", [])
                    ],
                ]
                semantics = validate_metric_batch(
                    [item.model_dump() for item in result.metrics],
                    operands,
                    require_fact_lineage=True,
                    requirements=requirements,
                )
                for item in result.metrics:
                    semantic = semantics[item.key]
                    lineage = []
                    for key in item.input_keys:
                        value = operands[key]
                        lineage.extend(value.get("inputs", [value]))
                    evidence = list(
                        dict.fromkeys(
                            i["evidence_id"] for i in lineage if "evidence_id" in i
                        )
                    )
                    if not evidence:
                        raise ValueError(
                            "Metric requires at least one authorized fact input"
                        )
                    operands[item.key] = {**semantic, "inputs": lineage}
                    mid = "M-" + digest([ident, item.key])[:16]
                    metrics[mid] = {
                        **{
                            k: semantic[k]
                            for k in (
                                "semantic_type",
                                "formula_id",
                                "numerator_basis",
                                "denominator_basis",
                                "is_estimate",
                                "display_precision",
                                "description",
                                "input_keys",
                            )
                        },
                        "schema_version": 2,
                        "rounding_error": semantic.get("rounding_error"),
                        "calculated_value": semantic.get("calculated_value"),
                        "comparison": semantic.get("comparison"),
                        "metric_id": mid,
                        "value": item.value,
                        "unit": item.unit,
                        "context": item.context,
                        "formula_version": "sandbox-code-v1",
                        "operation": "execute",
                        "execution_id": ident,
                        "local_key": item.key,
                        "inputs": lineage,
                        "dependency_keys": [
                            k
                            for k in item.input_keys
                            if k not in inputs and k not in result.assumptions
                        ],
                        "evidence_ids": evidence,
                        "checks": result.checks,
                        "limitations": result.limitations,
                        "assumptions": {
                            k: v.model_dump() for k, v in result.assumptions.items()
                        },
                        "independently_recomputed": False,
                    }
                key_to_id = {m["local_key"]: mid for mid, m in metrics.items()}
                for path in result.charts:
                    chart = ChartManifest.model_validate_json(files[path])
                    base = Path(path).parent

                    def get(file):
                        return files[(base / relative_path(file)).as_posix()]

                    data = json.loads(get(chart.data_file))
                    validate_png(get(chart.static_file), chart.width, chart.height)
                    if chart.option_file:
                        validate_option(json.loads(get(chart.option_file)))
                    if chart.svg_file:
                        validate_svg(get(chart.svg_file))
                    if (
                        not set(chart.metric_keys) <= key_to_id.keys()
                        or not set(chart.input_keys) <= inputs.keys()
                    ):
                        raise ValueError("Chart references unknown metric/fact input")
                    metric_ids = [key_to_id[key] for key in chart.metric_keys]
                    evidence = list(
                        dict.fromkeys(
                            [
                                *(
                                    e
                                    for mid in metric_ids
                                    for e in metrics[mid]["evidence_ids"]
                                ),
                                *(inputs[k]["evidence_id"] for k in chart.input_keys),
                            ]
                        )
                    )
                    if not evidence or not set(chart.evidence_ids) <= set(evidence):
                        raise ValueError("Chart evidence lineage missing or forged")
                    if chart.kind == "candlestick" and not chart.input_keys:
                        raise ValueError("Candlesticks require authorized price inputs")
                    if chart.kind == "sankey" and (
                        not data.get("flow_basis") or not data.get("links")
                    ):
                        raise ValueError("Flow chart lacks real relations")
                    cid = "C-" + digest([ident, path])[:16]
                    chart_files = [
                        path,
                        (base / chart.data_file).as_posix(),
                        (base / chart.static_file).as_posix(),
                    ]
                    chart_files += [
                        (base / p).as_posix()
                        for p in (chart.option_file, chart.svg_file)
                        if p
                    ]
                    charts[cid] = {
                        "receipt": {
                            **chart.model_dump(),
                            "chart_id": cid,
                            "execution_id": ident,
                            "metric_ids": metric_ids,
                            "evidence_ids": evidence,
                        },
                        "manifest_path": f"{record['root']}/output/{path}",
                        "files": {
                            f"{record['root']}/output/{p}": sha(files[p])
                            for p in chart_files
                        },
                    }
            # Validate every candidate before publishing anything or issuing an ID.
            for path, raw in files.items():
                target = output / path
                immutable_write(target, raw)
                record["files"][target.relative_to(root).as_posix()] = sha(raw)
            response = {
                **record["response"],
                "status": "registered" if "result.json" in files else "completed",
                "metric_ids": list(metrics),
                "metric_key_to_id": {m["local_key"]: mid for mid, m in metrics.items()},
                "chart_ids": list(charts),
                "files": [
                    "/workspace/" + record["root"] + "/output/" + p for p in files
                ],
            }
            record.update(
                status=response["status"],
                response=response,
                registered_at=datetime.fromtimestamp(
                    record["started_at"] + record["seconds"], timezone.utc
                ).isoformat(),
            )
            if "result.json" in files:
                record["assumptions"] = {
                    k: v.model_dump() for k, v in result.assumptions.items()
                }
            receipt_path = root / record["root"] / "execution.json"
            encoded = canonical(record).encode()
            immutable_write(receipt_path, encoded)
            record["files"][receipt_path.relative_to(root).as_posix()] = sha(encoded)
            response["receipt_path"] = (
                "/workspace/" + receipt_path.relative_to(root).as_posix()
            )

            def publish(state):
                from research_copilot.runtime.rework import validate_dispatch

                validate_dispatch(state, state.dispatches[scope.dispatch_id])
                state.role_data.setdefault("metrics", {}).setdefault(
                    scope.dispatch_id, {}
                ).update(metrics)
                state.role_data.setdefault("charts", {}).setdefault(
                    scope.dispatch_id, {}
                ).update(charts)
                state.role_data["executions"][ident] = json.loads(canonical(record))
                paths = state.role_data["scopes"][scope.dispatch_id]["paths"]
                paths.update({"/workspace/" + p: h for p, h in record["files"].items()})

            await self.control.acheck()
            await self.services.repository.mutate_control(context, publish)
            return response
        except (ValueError, KeyError, TypeError) as exc:
            # A malformed candidate is a completed failed execution; a new edited call is allowed.
            record["status"] = "invalid_result"
            record["response"] = {
                **record["response"],
                "status": "invalid_result",
                "error": str(exc),
                "metric_ids": [],
                "chart_ids": [],
            }
            await self._save(context, ident, record)
            return record["response"]

    def _validate_inputs(self, result, state, scope, record):
        inputs = {}
        authorized = state.role_data["scopes"][scope.dispatch_id]["evidence_ids"]
        for item in result.input_refs:
            if item.evidence_id not in authorized:
                raise PermissionError("Execution cites unauthorized evidence")
            source = state.role_data["evidence"][item.evidence_id]
            if (
                item.content_hash != source["content_hash"]
                or item.content_hash != record["evidence_hashes"].get(item.evidence_id)
                or digest(source["raw"]) != item.content_hash
            ):
                raise ValueError("Execution evidence hash mismatch")
            raw = source["raw"]
            from research_copilot.subagents.facts import document as source_document

            document = source_document(raw)
            from research_copilot.subagents.facts import validate_source_metadata

            validate_source_metadata(
                raw, item.pointer, currency=item.currency, basis=item.basis
            )
            value = document
            if item.pointer:
                if not item.pointer.startswith("/"):
                    raise ValueError("Invalid JSON pointer")
                for part in item.pointer[1:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    value = value[int(part)] if isinstance(value, list) else value[part]
            if item.text_start is not None:
                if (
                    not isinstance(value, str)
                    or item.text_end is None
                    or item.text_end <= item.text_start
                    or value[item.text_start : item.text_end] != item.raw_value
                    or item.extraction != "text-extraction"
                ):
                    raise ValueError("Text extraction does not match source offsets")
            else:
                if (
                    isinstance(value, (dict, list, bool))
                    or str(value) != item.raw_value
                ):
                    raise ValueError("Structured input differs from original value")
                metadata = (
                    document.get("context", document.get("meta", {}))
                    if isinstance(document, dict)
                    else {}
                )
                for key in ("entity", "period", "unit", "basis", "scale", "currency"):
                    expected = metadata.get(
                        key,
                        "1"
                        if key == "scale"
                        else metadata.get("unit")
                        if key == "currency"
                        and metadata.get("unit") in {"CNY", "USD", "EUR", "HKD", "JPY"}
                        else None,
                    )
                    if expected is None or str(expected) != getattr(item, key):
                        raise ValueError("Unverified structured input context: " + key)
            inputs[item.key] = item.model_dump()
        return inputs
