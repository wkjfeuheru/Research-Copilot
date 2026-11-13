"""Autonomous analysis tools. Calculations and charts come from sandbox receipts."""

from langchain.tools import ToolRuntime, tool
from research_copilot.contracts import RunContext

from research_copilot.subagents.contracts import (
    AnalysisCandidate,
    BusinessInputError,
    AssumptionSpec,
    MetricSpec,
)


class AnalysisService:
    def __init__(self, services, *, execution=None):
        self.services = services
        self.execution = execution

    async def saved_result(self, context):
        """A saved artifact already is the result; do not ask the model to copy it."""
        from research_copilot.contracts import ArtifactRef
        from research_copilot.subagents.contracts import AnalysisResult

        scope, state = await self.services.scope(context, "analysist")
        ref = getattr(state, "artifacts", {}).get("analysis")
        obj = state.role_data.get("objects", {}).get(ref.artifact_id) if ref else None
        if not obj or obj["dispatch_id"] != scope.dispatch_id:
            return None
        obj = await self.services.get_object(context, ref.artifact_id, kind="analysis")
        payload = obj["payload"]
        return AnalysisResult(
            task_id=state.role_data["scopes"][scope.dispatch_id]["task_brief"][
                "task_id"
            ],
            status="completed",
            summary=payload["summary"][:1200],
            evidence_ids=payload["evidence_ids"],
            metric_ids=payload["metric_ids"],
            chart_ids=payload["chart_ids"],
            analysis_id=ref.artifact_id,
            artifact_refs=[ArtifactRef.model_validate(obj["ref"])],
            assumptions=payload.get("assumptions", []),
            limitations=payload.get("limitations", []),
        )

    def _metric_operands(self, scope, state):
        """Authorized fact InputRefs from the dispatch's projected fact sheets."""
        material = state.role_data["scopes"][scope.dispatch_id]
        operands = {}
        for sheet_id in material.get("fact_sheet_ids", []):
            sheet = state.role_data["fact_sheets"][sheet_id]
            if not set(sheet["evidence_ids"]) <= set(material["evidence_ids"]):
                raise PermissionError("Fact sheet exceeds evidence scope")
            for fact in sheet["facts"]:
                operands.setdefault(fact["input_ref"]["key"], fact["input_ref"])
        return operands

    def _validate_specs(
        self, spec_dicts, assumption_keys, operand_keys, *, requirements=()
    ):
        from research_copilot.subagents.metric_quality import (
            SUPPORTED_FORMULAS,
            canonical_semantic,
            canonical_unit,
        )

        keys = [spec["key"] for spec in spec_dicts]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate metric spec key")
        collisions = sorted(set(keys) & (set(operand_keys) | set(assumption_keys)))
        if collisions:
            raise ValueError(f"Metric spec keys collide with inputs: {collisions}")
        declared = set(operand_keys) | set(assumption_keys)
        for index, spec in enumerate(spec_dicts):
            if spec["formula_id"] not in SUPPORTED_FORMULAS:
                raise ValueError(
                    f"Metric {spec['key']}: formula_id {spec['formula_id']} is not "
                    "declarative; write sandbox code via execute for it instead"
                )
            resolvable = declared | set(keys[:index])
            unknown = [k for k in spec["input_keys"] if k not in resolvable]
            if unknown:
                raise ValueError(
                    f"Metric {spec['key']} references unknown inputs: {unknown}"
                )
        declared_outputs = {
            (
                s["key"],
                canonical_semantic(s["semantic_type"]),
                canonical_unit(s["unit"]),
            )
            for s in spec_dicts
        }
        missing = [
            r
            for r in requirements
            if (
                r["key"],
                canonical_semantic(r["semantic_type"]),
                canonical_unit(r["unit"]),
            )
            not in declared_outputs
        ]
        if missing:
            # 列出声明的口径，避免逐条试错（WLY-20261108 第十一轮 analysist 两次整批被拒）。
            detail = "；".join(
                f"{item['key']}（semantic_type={item['semantic_type']}, "
                f"unit={item['unit']}）"
                for item in missing
            )
            raise ValueError(
                "同一批次须依序提交中间计算和全部计划指标，尚缺：" + detail
            )

    async def run_metric_specs(self, context, tool_call_id, metrics, assumptions):
        """Freeze authorized inputs, then compute declared specs via the fixed sandbox runner."""
        spec_dicts = [metric.model_dump() for metric in metrics]
        assumption_dicts = {
            item.key: {"value": item.value, "basis": item.basis, "impact": item.impact}
            for item in assumptions
        }
        scope, state = await self.services.scope(context, "analysist")
        if state.role_data["scopes"][scope.dispatch_id].get("submission_repair"):
            raise BusinessInputError(
                "submission-only repair must reuse registered receipts; recalculation requires a separate authorized task"
            )
        operands = self._metric_operands(scope, state)
        self._validate_specs(
            spec_dicts,
            set(assumption_dicts),
            set(operands),
            requirements=(
                state.role_data["scopes"][scope.dispatch_id].get("task_brief") or {}
            ).get("required_metrics", []),
        )
        from decimal import InvalidOperation
        from research_copilot.subagents.metric_quality import preflight_specs

        try:
            preflight_specs(
                spec_dicts,
                {
                    **operands,
                    **{
                        key: {"assumption_key": key, **item}
                        for key, item in assumption_dicts.items()
                    },
                },
            )
        except (ValueError, InvalidOperation, ZeroDivisionError) as exc:
            raise BusinessInputError(
                "Metric specification rejected before execution: " + str(exc)
            ) from exc
        execution = (
            self.execution
            if self.execution is not None
            else self.services.sandbox_execution
        )
        if execution is None:
            raise PermissionError("Sandbox execution service is unavailable")
        from research_copilot.subagents.services import digest, immutable_write
        from research_copilot.workspace.filesystem import reject_links

        used, stack = set(), [k for spec in spec_dicts for k in spec["input_keys"]]
        spec_index = {spec["key"]: spec for spec in spec_dicts}
        while stack:
            item = stack.pop()
            if item in used:
                continue
            used.add(item)
            if item in spec_index:
                stack.extend(spec_index[item]["input_keys"])
        document = {
            "schema_version": 1,
            "inputs": {k: operands[k] for k in sorted(used) if k in operands},
            "assumptions": {
                k: assumption_dicts[k] for k in sorted(used) if k in assumption_dicts
            },
            "metrics": spec_dicts,
        }
        from research_copilot.subagents.services import canonical

        filename = f"spec-inputs-{digest([tool_call_id, document])[:16]}.json"
        root = self.services.report_root(context)
        target = reject_links(
            root / scope.write_prefix.removeprefix("/workspace/") / filename
        )
        immutable_write(target, canonical(document).encode("utf-8"))
        command = (
            "python /skills/analysist/financial-modeling/scripts/spec_runner.py "
            f"{filename}"
        )
        response = await execution.execute(context, tool_call_id, command)
        registered = response.get("status") == "registered"
        return {
            "ok": registered,
            "execution_id": response.get("execution_id"),
            "status": response.get("status"),
            "metric_key_to_id": response.get("metric_key_to_id", {}),
            "metric_ids": response.get("metric_ids", []),
            "output_preview": (response.get("output") or "")[:1500],
            "error": response.get("error"),
        }

    async def save_candidate(self, context, tool_call_id, candidate_path):
        candidate = AnalysisCandidate.model_validate_json(
            await self.services.candidate(context, candidate_path, "analysist")
        )
        return await self._publish_candidate(context, tool_call_id, candidate)

    async def publish_structured(
        self, context, candidate: AnalysisCandidate, submission_key
    ):
        """Structured-submission path: the AnalysisResult.candidate IS the artifact."""
        return await self._publish_candidate(context, submission_key, candidate)

    async def _publish_candidate(self, context, submission_identity, candidate):
        scope, state = await self.services.scope(context, "analysist")
        grants = state.role_data["scopes"][scope.dispatch_id]
        if not set(candidate.evidence_ids) <= set(grants["evidence_ids"]):
            raise PermissionError("Analysis cites unauthorized evidence")
        metrics = state.role_data.get("metrics", {}).get(scope.dispatch_id, {})
        if not set(candidate.metric_ids) <= metrics.keys():
            raise ValueError("Unknown metric receipt")
        from research_copilot.subagents.metric_quality import (
            validate_registered_metrics,
        )

        try:
            validate_registered_metrics(metrics, candidate.metric_ids)
        except ValueError as exc:
            raise BusinessInputError(
                "Analysis calculation validation: " + str(exc)
            ) from exc
        if candidate.metric_ids and not candidate.claims:
            raise ValueError("Quantitative analysis requires structured claims")
        if len({c.claim_id for c in candidate.claims}) != len(candidate.claims):
            raise ValueError("Duplicate analysis claim_id")
        for claim in candidate.claims:
            if (
                not (claim.metric_ids or claim.evidence_ids)
                or not set(claim.metric_ids) <= set(candidate.metric_ids)
                or not set(claim.evidence_ids) <= set(candidate.evidence_ids)
            ):
                raise ValueError("Analysis claim lineage is incomplete")
        from research_copilot.subagents.numeric_lineage import validate_analysis_numbers

        facts = [
            fact
            for ident in grants.get("fact_sheet_ids", [])
            for fact in state.role_data["fact_sheets"][ident]["facts"]
        ]
        evidence = {
            ident: state.role_data["evidence"][ident]
            for ident in candidate.evidence_ids
        }
        if errors := validate_analysis_numbers(candidate, metrics, evidence, facts):
            raise BusinessInputError("Analysis numeric lineage: " + "; ".join(errors))
        for ident in candidate.metric_ids:
            if metrics[ident].get("schema_version") != 2:
                raise ValueError(
                    "New analysis requires semantic metric schema_version=2"
                )
        charts = state.role_data.get("charts", {}).get(scope.dispatch_id, {})
        if not set(candidate.chart_ids) <= charts.keys():
            raise ValueError("Unknown chart receipt")
        for ident in candidate.metric_ids:
            execution_id = metrics[ident].get("execution_id")
            if execution_id:
                from research_copilot.sandbox.receipts import verify_execution

                from research_copilot.subagents.analysis_recovery import (
                    validate_receipt_origin,
                )

                execution = validate_receipt_origin(
                    state, scope.dispatch_id, ident, "metrics"
                )
                if not execution or ident not in execution["response"]["metric_ids"]:
                    raise ValueError("Invalid metric execution receipt")
                verify_execution(self.services.report_root(context), execution)
            if not set(metrics[ident]["evidence_ids"]) <= set(candidate.evidence_ids):
                raise ValueError("Analysis omits metric evidence lineage")
        import hashlib
        from research_copilot.workspace.filesystem import reject_links

        for ident in candidate.chart_ids:
            chart = charts[ident]
            execution_id = chart["receipt"].get("execution_id")
            if execution_id:
                from research_copilot.sandbox.receipts import verify_execution

                from research_copilot.subagents.analysis_recovery import (
                    validate_receipt_origin,
                )

                execution = validate_receipt_origin(
                    state, scope.dispatch_id, ident, "charts"
                )
                if not execution or ident not in execution["response"]["chart_ids"]:
                    raise ValueError("Invalid chart execution receipt")
                verify_execution(self.services.report_root(context), execution)
            for path, expected in chart["files"].items():
                if (
                    hashlib.sha256(
                        reject_links(
                            self.services.report_root(context) / path
                        ).read_bytes()
                    ).hexdigest()
                    != expected
                ):
                    raise ValueError("Chart hash mismatch before analysis submission")
            if not set(chart["receipt"]["metric_ids"]) <= set(
                candidate.metric_ids
            ) or not set(chart["receipt"]["evidence_ids"]) <= set(
                candidate.evidence_ids
            ):
                raise ValueError("Analysis omits chart input lineage")
        return await self.services.publish_object(
            context,
            submission_identity,
            "analysis",
            {**candidate.model_dump(), "verified_facts": facts},
        )


def build_analysis_tools(services, *, execution=None):
    service = AnalysisService(services, execution=execution)

    @tool
    async def submit_analysis(
        candidate: AnalysisCandidate, runtime: ToolRuntime[RunContext]
    ) -> dict:
        """Submit concise conclusions and registered metric IDs. Omit intermediate definitions and raw tables. The host returns the final AnalysisResult after saving; no duplicate final JSON is needed."""
        return await service.publish_structured(
            runtime.context, candidate, runtime.tool_call_id
        )

    @tool
    async def run_metric_specs(
        metrics: list[MetricSpec],
        assumptions: list[AssumptionSpec],
        runtime: ToolRuntime[RunContext],
    ) -> dict:
        """Submit declared metric specs; the host freezes authorized inputs and computes them in the sandbox via the fixed runner. Returns metric_key_to_id receipts."""
        return await service.run_metric_specs(
            runtime.context, runtime.tool_call_id, metrics, assumptions
        )

    @tool
    async def save_analysis(
        candidate_path: str, runtime: ToolRuntime[RunContext]
    ) -> dict:
        """Validate and submit analysis JSON from the assigned task directory; does not approve it."""
        return await service.save_candidate(
            runtime.context, runtime.tool_call_id, candidate_path
        )

    @tool
    async def get_analysis(analysis_id: str, runtime: ToolRuntime[RunContext]) -> dict:
        """Read an authorized immutable analysis and verify its hash."""
        return await services.get_object(runtime.context, analysis_id, kind="analysis")

    # Native FilesystemMiddleware supplies execute only on the Analysist sandbox backend.
    return [get_analysis, run_metric_specs, save_analysis, submit_analysis]
