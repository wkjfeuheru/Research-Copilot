"""Authenticated, serialized entry to a report's graph.

The store supplies durable task records, cross-process locks and business-specific
resume validation. UI approval data never goes directly to Command(resume=...).
https://docs.langchain.com/oss/python/langgraph/interrupts
"""

import inspect
import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import uuid4

from langgraph.types import Command

from research_copilot.contracts import CONFIRMATION_TOOLS, RunContext
from research_copilot.runtime.control import RunCancelled, RunControl

from research_copilot.runtime.identity import RUNTIME_VERSION, checkpoint_thread_id

# 这些失败码表示流程主动阻断（预算耗尽、执行结果未知等），必须人工处置，
# 不能靠续跑绕过。其余失败（早停、崩溃、服务中断）在账目已结清时可继续。
MANUAL_RECOVERY_CODES = frozenset({"WorkflowBlocked", "ExecutionUnknown"})


@dataclass(frozen=True, slots=True)
class SessionRecord:
    report_id: str
    actor_id: str
    runtime_version: str
    policy_version: str
    budget_id: str
    run_id: str = ""
    status: str = "created"
    cancel_requested: bool = False
    user_request: str = ""
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class SessionStatus:
    report_id: str
    status: str
    interrupts: tuple[dict, ...] = ()
    error_detail: str | None = None
    correction_id: str | None = None


class SessionStore(Protocol):
    async def read_control(self, context): ...
    async def mutate_control(self, context, operation): ...
    async def load_budget(self, context): ...
    async def load(self, report_id: str, actor_id: str) -> SessionRecord: ...
    def lock(self, report_id: str): ...
    async def save(self, record: SessionRecord) -> None: ...
    async def request_cancel(self, report_id: str, actor_id: str) -> None: ...
    async def validate_resume(
        self, record: SessionRecord, actor_id: str, pending: dict, decisions: dict
    ) -> None:
        """Validate identity, current versions/hashes and review feedback.

        Store approved decisions/receipts idempotently as needed by confirmation
        services. Reject stale snapshots even if the interrupt ID is still valid.
        """
        ...


def validate_decisions(pending: dict, supplied: dict) -> dict:
    if not supplied or set(supplied) != set(pending):
        raise ValueError("Decisions must match all current interrupt IDs")
    for ident, value in supplied.items():
        payload = pending[ident]
        if not isinstance(payload, dict) or not isinstance(value, dict):
            raise ValueError("Invalid interrupt payload")  # noqa: TRY004 - public decision contract
        if payload.get("kind") == "clarification":
            answer = value.get("answer")
            if (
                set(value) != {"answer"}
                or not isinstance(answer, str)
                or not answer.strip()
                or len(answer) > 8000
            ):
                raise ValueError(
                    "Clarification requires a nonempty answer of at most 8000 characters"
                )
        elif payload.get("kind") == "reviewer_deep_review":
            if value.get("decision") not in {"approve", "request_changes", "reject"}:
                raise ValueError("Invalid reviewer decision")
            if (
                value["decision"] != "approve"
                and not str(value.get("comment", "")).strip()
            ):
                raise ValueError("Reviewer feedback is required")
        elif "action_requests" in payload:
            actions = payload["action_requests"]
            decisions = value.get("decisions")
            if not isinstance(decisions, list) or len(decisions) != len(actions):
                raise ValueError("Incorrect tool decision count")
            for action, decision in zip(actions, decisions, strict=True):
                if action["name"] not in CONFIRMATION_TOOLS:
                    raise ValueError("Unsupported tool approval")
                if decision.get("type") not in {"approve", "reject"}:
                    raise ValueError("Only approve/reject decisions are allowed")
        else:
            raise ValueError("Unsupported interrupt kind")
    return supplied


class SessionService:
    def __init__(
        self, *, store: SessionStore, agent_factory, policy_version: str, on_event=None,
        source_statuses=None, source_checked_at=None,
    ):
        self.store, self.agent_factory = store, agent_factory
        self.policy_version, self.on_event = policy_version, on_event
        self._controls: dict[str, RunControl] = {}
        self._stopping = False
        self.source_statuses = source_statuses
        self.source_checked_at = source_checked_at

    def stop_runs(self):
        self._stopping = True
        for control in self._controls.values():
            control.stop()

    async def execute_command(self, job, *, before_execute):
        """Revalidate a durable command under the same report lock as direct calls.

        The callback commits ownership before any graph execution or correction
        grant. Admission does not replace checkpoint or business validation.
        """
        from research_copilot.runtime.run_commands import (
            CorrectionPayload,
            MessagePayload,
            ResumePayload,
            StartPayload,
            parse_command,
        )

        payload = parse_command(job)
        if self._stopping:
            raise RuntimeError("Session service is stopping")
        async with self.store.lock(job["report_id"]):
            record = await self._load(job["report_id"], job["actor_id"])
            if record.run_id != job["run_id"] or record.budget_id != job["budget_id"]:
                raise ValueError("Queued command run/budget identity changed")
            if record.cancel_requested:
                raise RunCancelled("Cancelled sessions cannot execute commands")
            if isinstance(payload, StartPayload) and not isinstance(
                payload, MessagePayload
            ):
                if record.user_request != payload.message:
                    raise ValueError("Queued start does not match the stored request")
            await self._require_writable(record)
            await before_execute()
            if isinstance(payload, MessagePayload):
                return await self._message(record, payload.message, payload.message_id)
            if isinstance(payload, StartPayload):
                return await self._start(record, payload.message)
            if isinstance(payload, ResumePayload):
                return await self._resume(record, payload.decision)
            if isinstance(payload, CorrectionPayload):
                await self._authorize_correction(record, payload.dispatch_call_id)
                return await self._resume(record, None)
            raise ValueError("Unsupported research command")

    def _config(self, report_id):
        return {
            "configurable": {"thread_id": checkpoint_thread_id(report_id)},
            "recursion_limit": 100,
        }

    async def _load(self, report_id, actor_id):
        record = await self.store.load(report_id, actor_id)
        if record.report_id != report_id or record.actor_id != actor_id:
            raise PermissionError("Session identity mismatch")
        if record.runtime_version != RUNTIME_VERSION:
            raise ValueError("V1 tasks must be handled by the V1 runtime")
        if record.policy_version != self.policy_version:
            raise ValueError("Session policy version changed")
        return record

    def _context(self, record):
        return RunContext(
            record.actor_id,
            record.report_id,
            record.run_id,
            record.budget_id,
            record.policy_version,
        )

    async def _agent(self, record, control):
        await self._require_writable(record)
        agent = self.agent_factory(record, control)
        return await agent if inspect.isawaitable(agent) else agent

    async def _require_writable(self, record):
        from research_copilot.runtime.autonomy import autonomous

        state = await self.store.read_control(self._context(record))
        if not autonomous(state):
            raise PermissionError(
                "Historical workflow is read-only; create a new report"
            )

    async def get_status(self, report_id, actor_id):
        record = await self._load(report_id, actor_id)
        from research_copilot.runtime.autonomy import autonomous

        state = await self.store.read_control(self._context(record))
        if not autonomous(state):
            return SessionStatus(report_id, "read_only")
        status_factory = getattr(self.agent_factory, "for_status", None)
        agent = (
            await status_factory(record, RunControl())
            if status_factory
            else await self._agent(record, RunControl())
        )
        snapshot = await agent.aget_state(self._config(report_id), subgraphs=True)
        pending = tuple(
            {"id": item.id, "value": item.value} for item in snapshot.interrupts
        )
        status = (
            "cancelled"
            if record.cancel_requested
            else "paused"
            if pending
            else record.status
        )
        error_detail = state.role_data.get("last_run_error", {}).get("message")
        return SessionStatus(report_id, status, pending, error_detail)

    async def authorize_correction(
        self, report_id, actor_id, expected_id, *, enqueue=False
    ):
        async with self.store.lock(report_id):
            record = await self._load(report_id, actor_id)
            return await self._authorize_correction(
                record, expected_id, enqueue=enqueue
            )

    async def _authorize_correction(self, record, expected_id, *, enqueue=False):
        await self._require_writable(record)
        raise ValueError(
            "Legacy planner correction is unavailable; use request/execution recovery"
        )

    async def message(self, report_id, actor_id, message, message_id):
        async with self.store.lock(report_id):
            record = await self._load(report_id, actor_id)
            return await self._message(record, message, message_id)

    async def _message(self, record, message, message_id):
        """Explicit user turn. It cannot approve an interrupt or extend a confirmed scope."""
        await self._require_writable(record)
        report_id = record.report_id
        from research_copilot.workspace import safe_component

        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise ValueError(
                "A non-empty message of at most 8000 characters is required"
            )
        safe_component(message_id)
        if not record.run_id:
            raise ValueError("会话尚未启动，请新建研究对话。")
        if record.cancel_requested:
            raise ValueError("Cancelled sessions cannot accept messages")
        state = await self.store.read_control(self._context(record))
        existing = next(
            (
                m
                for m in state.role_data.get("conversation", [])
                if m["id"] == message_id
            ),
            None,
        )
        if existing:
            if existing["role"] != "user" or existing["content"] != message.strip():
                raise ValueError("Message identity was reused with different content")
            return SessionStatus(report_id, record.status)
        agent = await self._agent(record, RunControl())
        snapshot = await agent.aget_state(self._config(report_id), subgraphs=True)
        if snapshot.interrupts:
            raise ValueError("请先在对话中处理待确认事项；普通消息不会替代批准或拒绝。")
        if state.approved.get("plan") or state.artifacts.get("plan"):
            raise ValueError("研究范围已冻结；请新建对话调整研究范围。")
        return await self._invoke(
            record,
            {
                "messages": [
                    {"role": "user", "content": message.strip(), "id": message_id}
                ]
            },
        )

    async def start(self, report_id, actor_id, message):
        async with self.store.lock(report_id):
            record = await self._load(report_id, actor_id)
            return await self._start(record, message)

    async def _start(self, record, message):
        await self._require_writable(record)
        from research_copilot.sources.health import require_available_sources

        require_available_sources(self.source_statuses)
        if not isinstance(message, str) or not message.strip() or len(message) > 8000:
            raise ValueError(
                "A non-empty message of at most 8000 characters is required"
            )
        if record.status != "created" or record.run_id:
            raise ValueError("Session already started; resume its pending interrupt")
        record = replace(record, run_id=uuid4().hex)
        return await self._invoke(
            record, {"messages": [{"role": "user", "content": message}]}
        )

    async def resume(self, report_id, actor_id, decision=None):
        async with self.store.lock(report_id):
            record = await self._load(report_id, actor_id)
            return await self._resume(record, decision)

    async def _resume(self, record, decision):
        await self._require_writable(record)
        report_id, actor_id = record.report_id, record.actor_id
        if record.cancel_requested:
            raise RunCancelled("Cancelled sessions cannot resume")
        agent = await self._agent(record, RunControl())
        snapshot = await agent.aget_state(self._config(report_id), subgraphs=True)
        pending = {item.id: item.value for item in snapshot.interrupts}
        # 已回答的 clarification 中断元数据会残留在 checkpoint（恢复后运行过早
        # 停止时不被清除），按 conversation 的 answer 消息排除，避免已答中断
        # 被当成待决事项要求重复回答（WLY-20261108 第十轮续跑失败）。
        if pending:
            answered_state = await self.store.read_control(self._context(record))
            answered_ids = {
                message["id"][len("answer:") :]
                for message in answered_state.role_data.get("conversation", [])
                if message["id"].startswith("answer:")
            }
            pending = {
                ident: value for ident, value in pending.items() if ident not in answered_ids
            }
        if not pending and snapshot.next and decision is None:
            # Explicit caller action resumes unfinished checkpoint nodes. No new
            # input, fresh budget or automatic replay of the complete report.
            return await self._invoke(record, None)
        if (
            decision is None
            and not pending
            and not snapshot.next
            and record.run_id
            and record.user_request
            and record.status in {"running", "failed"}
            and getattr(snapshot, "created_at", False) is None
        ):
            # A crash can occur after saving run_id but before the graph accepts its first input.
            # Require both journals to be pristine before presenting that same input again.
            context = self._context(record)
            state = await self.store.read_control(context)
            budget = await self.store.load_budget(context)
            history = state.role_data.get("conversation", [])
            pristine = (
                not snapshot.tasks
                and not any(snapshot.values.values())
                and not any(state.model_dump(exclude={"role_data"}).values())
                and state.role_data.keys()
                <= {"workflow_version", "conversation", "last_run_error"}
                and all(
                    m.get("id") == "user:" + report_id
                    and m.get("role") == "user"
                    and m.get("content") == record.user_request
                    for m in history
                )
                and not budget.calls
                and not budget.requests
                and not budget.charged_tokens
            )
            if not pristine:
                raise ValueError("首个检查点缺失，但已有运行或用量记录，需要人工核查。")
            return await self._invoke(
                record,
                {"messages": [{"role": "user", "content": record.user_request}]},
            )
        if (
            not pending
            and not snapshot.next
            and decision is None
            and record.status == "failed"
        ):
            state = await self.store.read_control(self._context(record))
            # 依据失败码判断能否续跑，而不是只认 IncompleteRun：一次失败的
            # 续跑会把 last_run_error 覆盖成该次异常类型，若只认 IncompleteRun
            # 报告会永久卡死无法恢复（WLY-20261108 第十轮）。
            if (
                state.role_data.get("last_run_error", {}).get("code")
                not in MANUAL_RECOVERY_CODES
            ):
                budget = await self.store.load_budget(self._context(record))
                if not budget.status()["usage_verified"] or any(
                    e.get("status") in {"started", "unknown", "output_ready"}
                    for e in state.role_data.get("executions", {}).values()
                ):
                    raise ValueError(
                        "Resolve unknown usage/execution before continuation"
                    )
                return await self._invoke(
                    record,
                    {
                        "messages": [
                            {
                                # 专属稳定 id：默认 user:<report_id> 会与首轮
                                # user_request 撞 ID 触发 identity 校验失败
                                # （WLY-20261108 第十轮续跑缺陷）。
                                "id": "resume:" + record.report_id,
                                "role": "user",
                                "content": "请继续当前冻结范围内尚未完成的研究；保留原任务身份与额度，必要时使用 ask_user，最终报告仍需签核。",
                            }
                        ]
                    },
                )
        validated = validate_decisions(pending, decision)
        await self.store.validate_resume(record, actor_id, pending, validated)
        return await self._invoke(record, Command(resume=validated))

    async def cancel(self, report_id, actor_id):
        await self._require_writable(await self._load(report_id, actor_id))
        await self.store.request_cancel(report_id, actor_id)
        if control := self._controls.get(report_id):
            control.cancel()
        if cancel := getattr(self.agent_factory, "cancel_report", None):
            await cancel(report_id)
        self._event(report_id, "cancel_requested")
        return await self.get_status(report_id, actor_id)

    def _event(self, report_id, event, **payload):
        if self.on_event:
            try:
                self.on_event({"report_id": report_id, "type": event, **payload})
            except Exception:
                logging.getLogger(__name__).exception("event delivery failed")

    async def _invoke(self, record, value):
        if record.cancel_requested:
            raise RunCancelled("Cancelled sessions cannot start")

        async def cancellation_probe():
            current = await self.store.load(record.report_id, record.actor_id)
            return current.cancel_requested

        control = RunControl(cancellation_probe=cancellation_probe)
        if self._stopping:
            control.stop()
        self._controls[record.report_id] = control
        record = replace(record, status="running")
        try:
            await self.store.save(record)
            from research_copilot.runtime.conversation import (
                append_message,
                assistant_messages,
            )

            if isinstance(value, dict):
                for message in value.get("messages", []):
                    if isinstance(message, dict) and message.get("role") == "user":
                        await self.store.mutate_control(
                            self._context(record),
                            lambda state, m=message: append_message(
                                state,
                                message_id=m.get("id", "user:" + record.report_id),
                                role="user",
                                content=m["content"],
                            ),
                        )
            await self.store.mutate_control(
                self._context(record),
                lambda state: state.role_data.pop("last_run_error", None),
            )
            self._event(record.report_id, "run_started", run_id=record.run_id)
            await control.acheck()
            agent = await self._agent(record, control)
            from research_copilot.runtime.source_audit import audit_context

            async def source_event(event):
                await self.store.mutate_control(
                    self._context(record),
                    lambda state: state.role_data.setdefault("source_audit", []).append(
                        event
                    ),
                )

            with audit_context(self._context(record), source_event):
                result = await agent.ainvoke(
                    value, self._config(record.report_id), context=self._context(record)
                )
            current = await self.store.load(record.report_id, record.actor_id)
            pending = tuple(
                {"id": item.id, "value": item.value}
                for item in result.get("__interrupt__", ())
            )
            status = (
                "cancelled"
                if current.cancel_requested
                else "paused"
                if pending
                else "idle"
            )
            replies = list(assistant_messages(result))

            def save_replies(state):
                state.role_data["todos"] = result.get("todos", [])
                for ident, text, _ in replies:
                    append_message(
                        state,
                        message_id=ident,
                        role="assistant",
                        content=text,
                        kind="text",
                    )

            await self.store.mutate_control(self._context(record), save_replies)
            state = await self.store.read_control(self._context(record))
            if not pending and not current.cancel_requested:
                if (
                    state.artifacts.get("report")
                    and state.approved.get("report") == state.artifacts["report"]
                ):
                    status = "idle"
                else:
                    status = "failed"
                    await self.store.mutate_control(
                        self._context(record),
                        lambda s: s.role_data.update(
                            last_run_error={
                                "code": "IncompleteRun",
                                "message": "主 Agent 已停止，但报告尚未签核；已有产物已保留。",
                            }
                        ),
                    )
            await self.store.save(replace(current, status=status))
            self._event(record.report_id, f"run_{status}", interrupts=pending)
            return SessionStatus(record.report_id, status, pending)
        except asyncio.CancelledError:
            # Model wrappers finish their settlement finally blocks before this
            # outer handler runs. Save a recoverable state while dependencies live.
            async def interrupted():
                await self.store.mutate_control(
                    self._context(record),
                    lambda state: state.role_data.update(
                        last_run_error={
                            "code": "RunInterrupted",
                            "message": "服务关闭中，任务已中断；恢复前请核查未确定请求。",
                        }
                    ),
                )
                current = await self.store.load(record.report_id, record.actor_id)
                await self.store.save(
                    replace(
                        current,
                        status="cancelled" if current.cancel_requested else "failed",
                    )
                )

            saving = asyncio.create_task(interrupted())
            try:
                await asyncio.shield(saving)
            except asyncio.CancelledError:
                await saving
            self._event(record.report_id, "run_interrupted")
            raise
        except Exception as exc:
            from research_copilot.runtime.conversation import public_error

            # 公开错误消息保持泛化；完整堆栈进服务日志供验收与运维诊断。
            logging.getLogger(__name__).exception(
                "Report %s run failed: %s", record.report_id, exc
            )
            error = {"code": type(exc).__name__, "message": public_error(exc)}
            await self.store.mutate_control(
                self._context(record),
                lambda state: state.role_data.__setitem__(
                    "last_run_error",
                    error,
                ),
            )
            current = await self.store.load(record.report_id, record.actor_id)
            status = (
                "cancelled"
                if isinstance(exc, RunCancelled) or current.cancel_requested
                else "failed"
            )
            await self.store.save(replace(current, status=status))
            self._event(
                record.report_id, f"run_{status}", error_type=type(exc).__name__
            )
            if status == "cancelled":
                return SessionStatus(record.report_id, status)
            raise
        finally:
            self._controls.pop(record.report_id, None)
