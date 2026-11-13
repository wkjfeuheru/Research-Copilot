"""Business-role composition; explicit legacy overrides retain scaffold support."""

from dataclasses import dataclass, field
from pathlib import Path
from weakref import WeakValueDictionary
from typing import Callable, Mapping, Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from research_copilot.contracts import AUTONOMOUS_ROLES, ROLE_NAMES, RunContext
from research_copilot.llm import MeteredChatModel
from research_copilot.orchestration import RoleBinding, build_supervisor
from research_copilot.subagents import build_placeholder_review_graph

from research_copilot.runtime.identity import checkpoint_thread_id
from research_copilot.runtime.source_contract import source_contract_compatible


@dataclass
class SupervisorFactory:
    models: Mapping[str, BaseChatModel]
    store: object
    business: object
    checkpointer: object
    workspace_root: Path
    resources_root: Path
    role_tools: Mapping[str, Sequence[BaseTool]] | None = None
    reviewer_factory: Callable | None = None
    role_services: object | None = None
    source_tools: Sequence = ()
    source_note: str = ""
    source_catalog: Mapping[str, Mapping] = field(default_factory=dict)
    source_contract: Mapping[str, object] = field(default_factory=dict)
    sandbox_config: object | None = None

    def __post_init__(self):
        from research_copilot.orchestration.status_reader import StatusReaders

        self._status_readers = StatusReaders()
        self._sandbox_services = WeakValueDictionary()
        if set(self.models) - {"summarizer"} != {"supervisor", *ROLE_NAMES}:
            raise ValueError("Exactly five model bindings are required")
        from research_copilot.llm.sdk import OpenAICompatibleChatModel

        if "summarizer" not in self.models and any(
            isinstance(m, OpenAICompatibleChatModel) for m in self.models.values()
        ):
            raise ValueError(
                "An explicit independent summarizer configuration is required for SDK models"
            )
        if (
            self.role_services is None
            and self.role_tools is None
            and self.reviewer_factory is None
        ):
            from research_copilot.subagents.services import RoleServices

            self.role_services = getattr(self.business, "role_services", None)
            if self.role_services is None:
                self.role_services = RoleServices(
                    repository=self.business.repository,
                    workspace_root=self.workspace_root,
                    business=self.business,
                )
        self.role_tools = dict(self.role_tools or {})
        if set(self.role_tools) - set(AUTONOMOUS_ROLES):
            raise ValueError("Unknown autonomous role tool configuration")
        if self.role_services is not None:
            if self.role_tools or self.reviewer_factory:
                raise ValueError(
                    "Business roles cannot be mixed with placeholder overrides"
                )
            if (
                self.role_services.business is not self.business
                or self.role_services.workspace_root != self.workspace_root
            ):
                raise ValueError(
                    "Role services must share the host business and workspace"
                )

    async def for_status(self, record, control):
        return await self(record, control, read_only=True)

    async def planning_state(self, record):
        checkpoint = await self.checkpointer.aget_tuple(
            {"configurable": {"thread_id": checkpoint_thread_id(record.report_id)}}
        )
        return (
            checkpoint.checkpoint.get("channel_values", {}).get("todos", [])
            if checkpoint
            else []
        )

    async def __call__(self, record, control, *, read_only=False):
        view = None
        if read_only:
            view = await self.store.read_snapshot(record.report_id, record.actor_id)
            if (view.record.policy_version, view.record.budget_id) != (
                record.policy_version,
                record.budget_id,
            ):
                raise PermissionError("Status session identity changed")
            record = view.record
        context = RunContext(
            record.actor_id,
            record.report_id,
            record.run_id,
            record.budget_id,
            record.policy_version,
        )

        def workflow_version(state):
            version = state.role_data.get("workflow_version")
            if version != "products-v2":
                raise PermissionError(
                    "Historical workflow is read-only; create a new report"
                )
            if not read_only and not source_contract_compatible(
                state, self.source_contract
            ):
                raise PermissionError(
                    "Historical source contract is read-only; create a new report"
                )
            return version

        # Freeze before configuration/model construction can fail. Historical
        # identity comes from journals, never the transient session status.
        version = (
            workflow_version(view.state)
            if view is not None
            else await self.business.repository.mutate_control(
                context, workflow_version
            )
        )
        budget = (
            view.budget if view is not None else await self.store.load_budget(context)
        )
        status_key = None
        if view is not None and view.state.role_data.get("workflow_version") == version:
            status_key = (
                record.actor_id,
                record.report_id,
                record.runtime_version,
                record.policy_version,
                record.budget_id,
                record.run_id,
                version,
            )
            cached = self._status_readers.get(status_key)
            if cached is not None:
                return cached
        # No await from this point through cache insertion: simultaneous status
        # requests coalesce after their authenticated snapshot reads. Execution
        # still builds fresh report-bound graphs and never uses this cache.
        sandbox = None

        def meter(name):
            candidate = self.models[name]
            capacity = candidate.profile or {}

            async def audit(event):
                from research_copilot.runtime.control import active_dispatch

                scope = active_dispatch.get()
                await self.role_services.repository.mutate_control(
                    context,
                    lambda state: state.role_data.setdefault("model_audit", []).append(
                        {
                            **event,
                            "role": name,
                            "dispatch_id": scope.dispatch_id if scope else None,
                            "run_id": context.run_id,
                        }
                    ),
                )

            return MeteredChatModel.wrap(
                candidate,
                budget=budget,
                budget_stage=name,
                max_input_tokens=capacity["max_input_tokens"],
                max_output_tokens=capacity["max_output_tokens"],
                request_audit=audit if self.role_services else None,
                before_request=control.acheck,
                output_parameter=getattr(candidate, "output_parameter", "max_tokens"),
            )

        reviewer_model = meter("reviewer")
        # The injected builder constructs a graph only; it must not execute it here.
        if self.role_services is not None:
            from research_copilot.subagents.searcher import build_search_tools
            from research_copilot.subagents.analysist import build_analysis_tools
            from research_copilot.subagents.writer import build_writing_tools
            from research_copilot.subagents.reviewer import (
                ReviewService,
                build_reviewer_graph,
            )
            from research_copilot.subagents.contracts import RESULT_TYPES
            from research_copilot.subagents.services import digest

            runtime_manifest = {
                "preflight": getattr(self.role_services, "preflight_result", None),
                "models": {
                    name: {
                        "model": getattr(candidate, "model_name", ""),
                        "provider": getattr(
                            candidate, "provider_id", candidate._llm_type
                        ),
                        "context_tokens": (candidate.profile or {}).get(
                            "max_input_tokens"
                        ),
                        "max_output_tokens": (candidate.profile or {}).get(
                            "max_output_tokens"
                        ),
                        "context_chars_per_token": getattr(
                            candidate, "context_chars_per_token", 2.0
                        ),
                        "generation_config_hash": digest(
                            {
                                "extra_body": getattr(candidate, "extra_body", {}),
                                "temperature": getattr(candidate, "temperature", None),
                                "output_parameter": getattr(
                                    candidate, "output_parameter", "max_tokens"
                                ),
                            }
                        ),
                    }
                    for name, candidate in self.models.items()
                },
                "resources_hash": digest(
                    (self.resources_root / "manifest.json").read_text("utf-8")
                ),
                "sources": dict(self.source_catalog)
                or {
                    source.name: {
                        "server": source.server,
                        "original_name": source.original_name,
                        "schema_hash": source.schema_hash or digest(source.schema),
                    }
                    for source in self.source_tools
                },
                "source_contract": dict(self.source_contract),
                "rules_hash": digest(self.role_services.report_rules),
                "context_policy": "checked-summary-v1-char-estimate-v1-handoff-v1",
            }
            from research_copilot.sandbox.config import SandboxConfig
            from research_copilot.sandbox.receipts import ExecutionService

            sandbox_config = self.sandbox_config or SandboxConfig.load()
            # Queue wait is host admission policy, outside the frozen execution contract.
            runtime_manifest["sandbox"] = sandbox_config.model_dump(
                exclude={"queue_seconds"}
            )
            sandbox = ExecutionService(
                self.role_services,
                config=sandbox_config,
                control=control,
                budget=budget,
                resources_root=self.resources_root,
                admission_check=self.store.check_external_admission,
                slot_claims=self.store.sandbox_slots,
            )
            if not read_only:
                self.role_services.runtime_manifest = runtime_manifest
                self._sandbox_services[(record.report_id, id(sandbox))] = sandbox

            review = ReviewService(
                self.role_services,
                quality_model=reviewer_model.model_copy(
                    update={"budget_branch": "quality"}
                ),
                compliance_model=reviewer_model.model_copy(
                    update={"budget_branch": "compliance"}
                ),
                control=control,
                resources_root=self.resources_root,
            )
            if not read_only:
                self.business.review_authorizer = review.authorize_resume
                self.role_services.resources_root = self.resources_root
            graph = build_reviewer_graph(review)
            tools = {
                "searcher": build_search_tools(self.role_services, self.source_tools),
                "analysist": build_analysis_tools(
                    self.role_services, execution=sandbox
                ),
                "writer": build_writing_tools(
                    self.role_services, resources_root=self.resources_root
                ),
            }
            roles = {
                name: RoleBinding(
                    meter(name), tools[name], response_schema=RESULT_TYPES[name]
                )
                for name in AUTONOMOUS_ROLES
            }
        else:
            graph = (
                self.reviewer_factory(
                    model=reviewer_model, context=context, business=self.business
                )
                if self.reviewer_factory is not None
                else build_placeholder_review_graph()
            )
            roles = {
                name: RoleBinding(
                    self.models[name],
                    self.role_tools.get(name, ()),
                    implemented=name in self.role_tools,
                )
                for name in AUTONOMOUS_ROLES
            }
        graph = build_supervisor(
            model=meter("supervisor"),
            roles=roles,
            review_graph=graph,
            review_models=[reviewer_model],
            report_id=record.report_id,
            workspace_root=self.workspace_root,
            resources_root=self.resources_root,
            checkpointer=self.checkpointer,
            business=self.business,
            control=control,
            budget=budget,
            sandbox_execution_service=sandbox,
            summary_model=meter("summarizer") if "summarizer" in self.models else None,
            autonomous_workflow=version == "products-v2",
            read_only=read_only,
            source_note=self.source_note,
        )
        if read_only:
            from research_copilot.orchestration.status_reader import StatusReader

            reader = StatusReader(graph, record.report_id)
            if status_key is not None:
                self._status_readers.put(status_key, reader)
            return reader
        return graph

    async def cancel_report(self, report_id):
        for (owner, _), service in list(self._sandbox_services.items()):
            if owner == report_id:
                await service.runner.cancel_report(report_id)
