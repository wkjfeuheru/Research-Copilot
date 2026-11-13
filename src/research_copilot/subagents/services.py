"""Authoritative, transaction-backed role material and acquisition services.

The host supplies authenticated repositories and external source adapters. Model
arguments never supply report identity, grants, dispatch identity or raw evidence.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from research_copilot.runtime.experience import LLMExperienceExtractor

from research_copilot.contracts import ArtifactRef, RoleResult
from research_copilot.runtime.control import active_dispatch
from research_copilot.workspace import safe_component
from research_copilot.workspace.filesystem import reject_links, tree_files

from research_copilot.runtime.identity import PROJECTION_FORMAT


class MissingSubmissionReceipt(ValueError):
    """Model output names an artifact that was never submitted."""


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def runtime_contract(manifest):
    """Manifest identity excluding the budget policy frozen in the ledger.

    Stage caps and global limits are snapshotted per report at creation, so a
    later policy-file change alters only new reports. Keeping the policy in the
    continuity check would permanently block resuming an in-flight report even
    when an operator has explicitly extended its stage ledger.
    """
    if not isinstance(manifest, dict):
        return manifest
    comparable = dict(manifest)
    preflight = comparable.get("preflight")
    if isinstance(preflight, dict):
        comparable["preflight"] = {
            key: value for key, value in preflight.items() if key != "execution_policy"
        }
    return comparable


DERIVED_FIELD_INPUTS = {
    "gross_margin": ("revenue", "operating_cost"),
    "gross_profit": ("revenue", "operating_cost"),
    "net_margin": ("net_profit_attributable", "revenue"),
}


def derives_from_provided(field, provided):
    """A ratio the analysist computes from collected inputs is not a gap."""
    match = re.fullmatch(r"(gross_margin|gross_profit|net_margin)_(\d{4})", field)
    if not match:
        return False
    prefix, year = match.groups()
    return all(f"{base}_{year}" in provided for base in DERIVED_FIELD_INPUTS[prefix])


def projection_bytes(value):
    """Readable projection only: long strings concatenate exactly, originals stay in storage.

    Deep Agents read_file paginates lines. A single-line JSON document would make
    its pagination ineffective for large evidence bodies.
    """

    def segment(item):
        if isinstance(item, str) and len(item) > 240:
            return {
                "text_segments": [
                    item[start : start + 240] for start in range(0, len(item), 240)
                ],
                "join": "",
            }
        if isinstance(item, dict):
            return {key: segment(value) for key, value in item.items()}
        if isinstance(item, list):
            return [segment(value) for value in item]
        return item

    return json.dumps(
        {"projection_format": PROJECTION_FORMAT, "value": segment(value)},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode()


def immutable_write(path, content):
    reject_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            with path.open("xb") as stream:
                stream.write(content)
            return
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError("Immutable material hash mismatch")
            return
        except PermissionError:
            # Windows 下并发派单或防病毒扫描会短暂独占新文件；重试而非砸穿运行。
            if attempt == 2:
                raise
            time.sleep(0.2 * (attempt + 1))


def execution_authorization(approved_policy, brief):
    """计划批准的执行额度与用途清单；图表/自定义任务补齐一次修复用途。

    图表分析包含脚本修复重试（run_metric_specs + 首跑 + 修复）。模型常把额度
    只写成用途数且用途清单散落在 brief 与计划两处（WLY-20261108 第五轮验收
    缺陷），故绑定 scope 时以 brief 声明为准合并，并以代码兜底额度。
    """
    limit = approved_policy.get("analysis_execution_limit", 2)
    purposes = list(
        dict.fromkeys(
            [*brief.execution_purposes, *approved_policy.get("execution_purposes", [])]
        )
    )
    if brief.calculation_mode == "custom":
        needed = len(brief.execution_purposes) + 1
        if needed > limit:
            limit = needed
        while len(purposes) < limit:
            purposes.append(
                f"{brief.execution_purposes[-1]}_repair"
                if brief.execution_purposes
                else f"execution_{len(purposes) + 1}"
            )
    return limit, purposes


def register_original(
    state,
    *,
    server,
    tool_name,
    arguments,
    raw,
    collected_at,
    kind,
    parent_id=None,
    schema_hash=None,
    identity_hash=None,
    provenance=None,
):
    data = state.role_data
    evidence = data.setdefault("evidence", {})
    content_hash = digest(raw)
    identity = digest(
        [
            server,
            tool_name,
            arguments,
            identity_hash or content_hash,
            parent_id,
            kind,
        ]
    )
    ident = data.setdefault("evidence_keys", {}).get(identity)
    if ident is None:
        ident = f"E-{len(evidence) + 1:06d}"
        source = {
            "server": server,
            "tool": tool_name,
            "arguments": arguments,
            "schema_hash": schema_hash,
            "historical_availability": "unknown",
        }
        if provenance:
            source["provenance"] = provenance
        evidence[ident] = {
            "evidence_id": ident,
            "content_hash": content_hash,
            "raw": raw,
            "kind": kind,
            "parent_id": parent_id,
            "collected_at": collected_at,
            "source": source,
        }
        data["evidence_keys"][identity] = ident
    return ident, content_hash


class RoleServices:
    def __init__(
        self,
        *,
        repository,
        workspace_root: Path,
        business,
        report_rules=None,
        experience=None,
    ):
        self.repository, self.workspace_root, self.business = (
            repository,
            workspace_root,
            business,
        )
        business.role_services = self
        self.experience = experience
        self._acquisition_locks: dict[tuple, asyncio.Lock] = {}
        self.runtime_manifest: dict[str, Any] = {}
        self.experience_extractor: LLMExperienceExtractor | None = None
        self.preflight_result: dict | None = None
        # Attached by the host factory; declarative metric specs run through it.
        self.sandbox_execution = None
        if report_rules is None:
            from research_copilot.runtime.report_rules import load_report_rules

            report_rules = load_report_rules(
                Path("config/compliance_rules.yaml")
            ).snapshot()
        self.report_rules = json.loads(canonical(report_rules))
        self.report_rules.setdefault("content_hash", digest(report_rules))

    def report_root(self, context):
        return reject_links(self.workspace_root / safe_component(context.report_id))

    async def register_attachment(self, context, reference):
        """Host-only import registration; never accepts model-generated original text."""
        import base64
        from research_copilot.workspace.inputs import _filename

        prefix = f"/workspace/inputs/{reference.content_hash}/"
        if (
            reference.report_id != context.report_id
            or not re.fullmatch(r"[a-f0-9]{64}", reference.content_hash)
            or not reference.path.startswith(prefix)
        ):
            raise PermissionError("Attachment belongs to another report")
        name = _filename(reference.path.removeprefix(prefix))
        path = reject_links(
            self.report_root(context) / "inputs" / reference.content_hash / name
        )
        raw = await asyncio.to_thread(path.read_bytes)
        if (
            len(raw) != reference.byte_size
            or hashlib.sha256(raw).hexdigest() != reference.content_hash
        ):
            raise ValueError("Attachment hash mismatch")
        try:
            body, encoding = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            body, encoding = base64.b64encode(raw).decode("ascii"), "base64"

        def register(state):
            ident, content_hash = register_original(
                state,
                server="attachment",
                tool_name="import_input",
                arguments={
                    "path": reference.path,
                    "original_hash": reference.content_hash,
                },
                raw={
                    "body": body,
                    "encoding": encoding,
                    "title": name,
                    "original_hash": reference.content_hash,
                },
                collected_at=datetime.now(timezone.utc).isoformat(),
                kind="attachment_original",
            )
            return {"ok": True, "evidence_id": ident, "content_hash": content_hash}

        return await self.repository.mutate_control(context, register)

    async def scope(self, context, role=None):
        dispatch = active_dispatch.get()
        if dispatch is None or (role and dispatch.role != role):
            raise PermissionError("Missing trusted role dispatch")
        state = await self.repository.read_control(context)
        binding = state.dispatches.get(dispatch.dispatch_id)
        if not binding or binding["role"] != dispatch.role:
            raise PermissionError("Dispatch is not authorized")
        if runtime_contract(
            state.role_data.get("runtime_manifest", {})
        ) != runtime_contract(self.runtime_manifest):
            raise ValueError("Runtime configuration changed during the report")
        if dispatch.role != "reviewer":
            from research_copilot.runtime.rework import validate_dispatch

            validate_dispatch(state, binding)
            from research_copilot.runtime.autonomy import autonomous

            if autonomous(state):
                current = state.artifacts.get("scope")
                if (
                    not current
                    or binding["inputs"].get("scope") != current.model_dump()
                ):
                    raise PermissionError("Dispatch scope authorization changed")
                if dispatch.role == "writer":
                    analysis = state.artifacts.get("analysis")
                    if (
                        not analysis
                        or binding["inputs"].get("analysis") != analysis.model_dump()
                    ):
                        raise PermissionError("Writer analysis version changed")
                return dispatch, state
            current = state.artifacts.get("plan")
            if (
                not current
                or binding["inputs"].get("plan") != current.model_dump()
                or state.approved.get("plan") != current
            ):
                raise PermissionError("Dispatch plan authorization changed")
        return dispatch, state

    async def prepare_dispatch(self, context, dispatch, description):
        from research_copilot.subagents.source_policy import parse_task_brief
        from research_copilot.subagents.contracts import BusinessInputError
        from research_copilot.runtime.experience import STAGE_BY_ROLE

        try:
            brief = parse_task_brief(description)
        except ValueError as exc:
            raise BusinessInputError(str(exc)) from exc
        if brief is None:
            raise BusinessInputError(
                "A valid TaskBrief with research as_of is required"
            )
        if brief.role != dispatch.role:
            raise BusinessInputError("TaskBrief role differs from trusted dispatch")

        before = await self.repository.read_control(context)
        existing_scope = before.role_data.get("scopes", {}).get(dispatch.dispatch_id)
        recalled = list(existing_scope.get("experiences", [])) if existing_scope else []
        if existing_scope is None and self.experience is not None:
            stage = STAGE_BY_ROLE.get(dispatch.role)
            if stage:
                recalled = await self.experience.recall(
                    dispatch.role,
                    stage,
                    self._known_problem_type(before, dispatch, brief),
                )
        effects = []

        def recall_info(experiences):
            ids = [item["experience_id"] for item in experiences]
            return {
                "experience_ids": ids,
                "experience_path": (
                    f"/workspace/artifacts/{dispatch.dispatch_id}/experiences.json"
                    if ids
                    else None
                ),
            }

        def prepare(state, budget_status=None):
            from research_copilot.runtime.autonomy import admit, autonomous

            if autonomous(state):
                if budget_status and budget_status.get("unknown_requests"):
                    raise BusinessInputError(
                        "Model usage is unknown; recover before dispatch"
                    )
                admit(state, dispatch, brief, description)
            if dispatch.dispatch_id not in state.dispatches:
                raise PermissionError("Dispatch is not authorized")
            data = state.role_data
            if brief:
                if data.setdefault("research_as_of", brief.as_of) != brief.as_of:
                    raise BusinessInputError(
                        "Task as_of differs from frozen report as_of"
                    )
            if runtime_contract(
                data.setdefault(
                    "runtime_manifest", json.loads(canonical(self.runtime_manifest))
                )
            ) != runtime_contract(self.runtime_manifest):
                raise ValueError("Runtime configuration changed during the report")
            if brief:
                # The approved plan freezes the authorization-relevant task
                # contract; the dispatch description may refine free-form wording.
                plan_ref = state.dispatches[dispatch.dispatch_id]["inputs"].get("plan")
                from research_copilot.subagents.task_authorization import (
                    validate_task_contract,
                )

                validate_task_contract(state, plan_ref, brief)
            scopes = data.setdefault("scopes", {})
            existing = scopes.get(dispatch.dispatch_id)
            if existing:
                if existing["description"] != description:
                    raise ValueError("Immutable task description changed")
                return recall_info(existing.get("experiences", []))
            plan_ref = state.dispatches[dispatch.dispatch_id]["inputs"].get("plan")
            plans = [p["plan"] for p in state.plans if p["artifact"] == plan_ref]
            approved_policy = plans[0] if len(plans) == 1 else {}
            if brief.max_facts > approved_policy.get("facts_per_task_limit", 15):
                raise BusinessInputError(
                    "max_facts exceeds the approved plan; split the work or confirm a larger plan"
                )
            approved_limit, approved_purposes = execution_authorization(
                approved_policy, brief
            )
            scopes[dispatch.dispatch_id] = {
                "analysis_execution_limit": approved_limit,
                "execution_purposes": approved_purposes,
                "description": description,
                "task_brief": brief.model_dump() if brief else None,
                "evidence_ids": list(data.get("evidence", {})),
                "object_ids": [
                    ref["artifact_id"]
                    for ref in state.dispatches[dispatch.dispatch_id]["inputs"].values()
                    if ref["artifact_id"] in data.get("objects", {})
                ],
                "paths": {},
                "fact_sheet_ids": [
                    key
                    for key, sheet in data.get("fact_sheets", {}).items()
                    if sheet["plan"]
                    == state.dispatches[dispatch.dispatch_id]["inputs"].get("plan")
                ],
                "experiences": recalled,
            }
            if autonomous(state) and brief.repair_of:
                original = scopes[brief.repair_of]
                scopes[dispatch.dispatch_id]["correction_of"] = brief.repair_of
                scopes[dispatch.dispatch_id]["evidence_ids"] = list(
                    original["evidence_ids"]
                )
                scopes[dispatch.dispatch_id]["source_calls"] = dict(
                    original.get("source_calls", {})
                )
                from research_copilot.subagents.analysis_recovery import (
                    bind_submission_repair,
                )

                bind_submission_repair(state, dispatch.dispatch_id, brief.repair_of)
            if autonomous(state):
                # 使用补齐后的用途清单（含图表修复用途），不能用 brief 原始声明
                # 覆盖——否则修复重试会因缺少用途被拒（WLY-20261108 第八轮缺陷）。
                scopes[dispatch.dispatch_id]["execution_purposes"] = list(
                    approved_purposes
                )
            for recovery in data.get("dispatch_recoveries", {}).values():
                if recovery.get("target_dispatch_id") == dispatch.dispatch_id:
                    original = recovery["original_dispatch_id"]
                    scopes[dispatch.dispatch_id]["correction_of"] = original
                    scopes[dispatch.dispatch_id]["evidence_ids"] = list(
                        scopes[original]["evidence_ids"]
                    )
                    break
            if recalled:
                effects.append(
                    {
                        "kind": "experience_recall",
                        "report_id": context.report_id,
                        "dispatch_id": dispatch.dispatch_id,
                        "role": dispatch.role,
                        "stage": STAGE_BY_ROLE[dispatch.role],
                        "experience_ids": [item["experience_id"] for item in recalled],
                    }
                )
            return recall_info(recalled)

        from research_copilot.runtime.autonomy import autonomous

        options = {"include_budget": True} if autonomous(before) else {}
        if self.experience is not None:
            info = await self.repository.mutate_control(
                context, prepare, effects=effects, **options
            )
        else:
            info = await self.repository.mutate_control(context, prepare, **options)
        await self.project(context, dispatch)
        return info or recall_info([])

    def _known_problem_type(self, state, dispatch, brief):
        if brief is not None and getattr(brief, "problem_type", None):
            return brief.problem_type
        ref = state.artifacts.get("rework")
        if ref is None:
            return None
        payload = state.role_data.get("rework", {}).get(ref.artifact_id)
        if not isinstance(payload, dict):
            return None
        tasks = [
            task
            for task in payload.get("tasks", [])
            if isinstance(task, dict) and task.get("role") == dispatch.role
        ]
        if not tasks:
            return None
        issue_ids = {
            issue_id
            for task in tasks
            for issue_id in task.get("issue_ids", [])
            if issue_id
        }
        review = state.role_data.get("reviews", {}).get(payload.get("review_id"))
        if not isinstance(review, dict):
            return None
        from research_copilot.runtime.experience import classify_problem_type

        tags = {
            classify_problem_type(issue.get("category"))
            for issue in review.get("issues", [])
            if isinstance(issue, dict) and issue.get("issue_id") in issue_ids
        }
        tags.discard(None)
        return tags.pop() if len(tags) == 1 else None

    async def project(self, context, dispatch):
        def publish(state):
            data = state.role_data
            scope = data["scopes"][dispatch.dispatch_id]
            index = []
            root = self.report_root(context)
            # Both roots are advertised to the role, even before its first artifact.
            for kind in ("evidence", "artifacts"):
                reject_links(root / kind / dispatch.dispatch_id).mkdir(
                    parents=True, exist_ok=True
                )
            # The immutable task must remain recoverable even if native history
            # compaction truncates its original delegation message.
            task_path = f"/workspace/artifacts/{dispatch.dispatch_id}/task.md"
            task_content = scope["description"].encode("utf-8")
            immutable_write(root / task_path.removeprefix("/workspace/"), task_content)
            scope["paths"][task_path] = hashlib.sha256(task_content).hexdigest()
            for ident in scope["evidence_ids"]:
                record = data["evidence"][ident]
                content = projection_bytes(record)
                path = f"/workspace/evidence/{dispatch.dispatch_id}/{ident}.json"
                immutable_write(root / path.removeprefix("/workspace/"), content)
                scope["paths"][path] = hashlib.sha256(content).hexdigest()
                from research_copilot.subagents.facts import source_segments

                segment_path = (
                    f"/workspace/evidence/{dispatch.dispatch_id}/{ident}.segments.jsonl"
                )
                segments = source_segments(record["raw"])
                immutable_write(
                    root / segment_path.removeprefix("/workspace/"), segments
                )
                scope["paths"][segment_path] = hashlib.sha256(segments).hexdigest()
                index.append(
                    {
                        "evidence_id": ident,
                        "path": path,
                        "content_hash": record["content_hash"],
                        "projection_hash": scope["paths"][path],
                        "kind": record["kind"],
                        "segments_path": segment_path,
                        "title": str(record["raw"].get("title", ""))[:160]
                        if isinstance(record["raw"], dict)
                        else "",
                        "source": record["source"]["server"],
                    }
                )
            fact_index = []
            for sheet_id in scope.get("fact_sheet_ids", []):
                sheet = data["fact_sheets"][sheet_id]
                if not set(sheet["evidence_ids"]) <= set(scope["evidence_ids"]):
                    raise PermissionError("Fact sheet exceeds evidence scope")
                path = f"/workspace/artifacts/{dispatch.dispatch_id}/{sheet_id}.json"
                content = json.dumps(sheet, ensure_ascii=False, indent=2).encode()
                immutable_write(root / path.removeprefix("/workspace/"), content)
                scope["paths"][path] = hashlib.sha256(content).hexdigest()
                fact_index.append(
                    {
                        "path": path,
                        "fact_count": len(sheet["facts"]),
                        "keys": [fact["input_ref"]["key"] for fact in sheet["facts"]],
                    }
                )
            path = f"/workspace/artifacts/{dispatch.dispatch_id}/fact_index.json"
            content = json.dumps(fact_index, ensure_ascii=False, indent=2).encode()
            reject_links(root / path.removeprefix("/workspace/")).write_bytes(content)
            scope["paths"][path] = hashlib.sha256(content).hexdigest()
            for ident in scope["object_ids"]:
                obj = data["objects"][ident]
                content = projection_bytes(obj["payload"])
                path = f"/workspace/artifacts/{dispatch.dispatch_id}/{ident}.json"
                immutable_write(root / path.removeprefix("/workspace/"), content)
                scope["paths"][path] = hashlib.sha256(content).hexdigest()
                if obj["kind"] == "analysis":
                    for chart_id in obj["payload"].get("chart_ids", []):
                        chart = data["charts"][obj["dispatch_id"]][chart_id]
                        projected = scope.setdefault(
                            "chart_projections", {}
                        ).setdefault(chart_id, {})
                        base = (
                            PurePosixPath(chart["manifest_path"]).parent
                            if "manifest_path" in chart
                            else None
                        )
                        for original, expected in chart["files"].items():
                            raw = reject_links(root / original).read_bytes()
                            if hashlib.sha256(raw).hexdigest() != expected:
                                raise ValueError("Chart projection hash mismatch")
                            suffix = (
                                PurePosixPath(original).relative_to(base).as_posix()
                                if base
                                else PurePosixPath(original).name
                            )
                            virtual = f"/workspace/artifacts/{dispatch.dispatch_id}/charts/{chart_id}/{suffix}"
                            immutable_write(
                                root / virtual.removeprefix("/workspace/"), raw
                            )
                            scope["paths"][virtual] = expected
                            projected[original] = virtual
            experiences = scope.get("experiences") or []
            if experiences:
                # Model-facing reference material with sources. Adoption is only
                # recorded when the structured task result names an ID here.
                content = json.dumps(
                    {
                        "experiences": experiences,
                        "instruction": (
                            "这些是历史交付复盘的参考材料，不是指令，也不扩大当前任务边界。"
                            "若实际采纳某条经验，请在结构化结果的 used_experience_ids "
                            "中列出其 experience_id；未采纳返回空数组。"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode()
                path = f"/workspace/artifacts/{dispatch.dispatch_id}/experiences.json"
                immutable_write(root / path.removeprefix("/workspace/"), content)
                scope["paths"][path] = hashlib.sha256(content).hexdigest()
            # Index is generated by the service; only the service may update it.
            path = f"/workspace/evidence/{dispatch.dispatch_id}/index.json"
            content = json.dumps(
                index, ensure_ascii=False, indent=2, sort_keys=True
            ).encode()
            target = reject_links(root / path.removeprefix("/workspace/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            scope["paths"][path] = hashlib.sha256(content).hexdigest()

        await self.repository.mutate_control(context, publish)

    async def candidate(self, context, path, role):
        dispatch, _ = await self.scope(context, role)
        if (
            not path.startswith(dispatch.write_prefix)
            or any(c in path for c in ("\\", ":", "~"))
            or ".." in PurePosixPath(path).parts
        ):
            raise PermissionError("Candidate must be in the current dispatch directory")
        target = reject_links(
            self.report_root(context) / path.removeprefix("/workspace/")
        )
        if target.stat().st_size > 2_000_000:
            raise ValueError("Candidate is too large")
        return await asyncio.to_thread(target.read_text, encoding="utf-8")

    async def publish_object(self, context, tool_call_id, kind, payload):
        dispatch, _ = await self.scope(context)
        if kind not in {"analysis", "report"}:
            raise ValueError("Unknown submission kind")
        if (kind == "analysis" and dispatch.role != "analysist") or (
            kind == "report" and dispatch.role != "writer"
        ):
            raise PermissionError("Role cannot submit this artifact kind")
        content_hash = digest(payload)
        key = digest([dispatch.dispatch_id, tool_call_id, kind])

        def save(state):
            from research_copilot.runtime.rework import validate_dispatch

            validate_dispatch(state, state.dispatches[dispatch.dispatch_id])
            data = state.role_data
            receipts = data.setdefault("submissions", {})
            if key in receipts:
                if receipts[key]["artifact"]["content_hash"] != content_hash:
                    raise ValueError("Submission replay content changed")
                return receipts[key]
            if "report" in state.approved:
                raise PermissionError("Signed report cannot be modified")
            bound = state.dispatches[dispatch.dispatch_id]["inputs"]
            from research_copilot.runtime.autonomy import autonomous

            scope_kind = "scope" if autonomous(state) else "plan"
            for required in (
                (scope_kind, "analysis") if kind == "report" else (scope_kind,)
            ):
                current = state.artifacts.get(required)
                if (
                    not current
                    or bound.get(required) != current.model_dump()
                    or (
                        not autonomous(state)
                        and state.approved.get(required) != current
                    )
                ):
                    raise PermissionError(
                        "Submission prerequisites changed or are not approved"
                    )
            for original in list(receipts.values()):
                ref = original["artifact"]
                obj = data.get("objects", {}).get(ref["artifact_id"])
                if (
                    obj
                    and obj["dispatch_id"] == dispatch.dispatch_id
                    and obj["kind"] == kind
                    and ref["content_hash"] == content_hash
                    and state.artifacts.get(kind) == ArtifactRef.model_validate(ref)
                ):
                    target = reject_links(
                        self.report_root(context)
                        / obj["path"].removeprefix("/workspace/")
                    )
                    if hashlib.sha256(target.read_bytes()).hexdigest() != content_hash:
                        raise ValueError("Submitted artifact hash mismatch")
                    receipts[key] = {**original, "receipt_id": key}
                    return receipts[key]
            if kind == "report":
                previous = state.artifacts.get("report")
                if bound.get("report") != (previous.model_dump() if previous else None):
                    raise ValueError("Concurrent draft version changed")
            version = str(
                1
                + len(
                    [
                        obj
                        for obj in data.get("objects", {}).values()
                        if obj["kind"] == kind
                    ]
                )
            )
            ident = f"{kind}-{version}-{content_hash[:16]}"
            ref = ArtifactRef(
                artifact_id=ident, version=version, content_hash=content_hash
            )
            path = f"/workspace/artifacts/store/{ident}.json"
            immutable_write(
                self.report_root(context) / path.removeprefix("/workspace/"),
                canonical(payload).encode(),
            )
            data.setdefault("objects", {})[ident] = {
                "kind": kind,
                "ref": ref.model_dump(),
                "payload": payload,
                "path": path,
                "dispatch_id": dispatch.dispatch_id,
            }
            data["scopes"][dispatch.dispatch_id]["object_ids"].append(ident)
            state.artifacts[kind] = ref
            state.approved.pop(kind, None)
            if kind in {"report", "analysis"}:
                state.reviewed_report = None
                state.approved.pop("report", None)
            receipt = {
                "ok": True,
                "receipt_id": key,
                "artifact": ref.model_dump(),
                "analysis_id" if kind == "analysis" else "draft_id": ident,
            }
            if kind == "report":
                receipt["draft_version"] = int(version)
            receipts[key] = receipt
            return receipt

        receipt = await self.repository.mutate_control(context, save)
        await self.project(context, dispatch)
        return receipt

    async def get_object(self, context, ident, *, kind=None):
        dispatch, state = await self.scope(context)
        scope = state.role_data["scopes"][dispatch.dispatch_id]
        if ident not in scope["object_ids"]:
            raise PermissionError("Artifact is outside authorized material snapshot")
        obj = state.role_data["objects"][ident]
        if kind and obj["kind"] != kind:
            raise ValueError("Unexpected artifact kind")
        content = reject_links(
            self.report_root(context) / obj["path"].removeprefix("/workspace/")
        ).read_bytes()
        if (
            hashlib.sha256(content).hexdigest() != obj["ref"]["content_hash"]
            or digest(obj["payload"]) != obj["ref"]["content_hash"]
        ):
            raise ValueError("Artifact hash mismatch")
        return obj

    async def validate_material_path(self, context, dispatch, path):
        if (
            not path.startswith("/")
            or any(c in path for c in ("\\", ":", "~"))
            or ".." in PurePosixPath(path).parts
        ):
            raise PermissionError("Invalid material path")
        state = await self.repository.read_control(context)
        scope = state.role_data["scopes"][dispatch.dispatch_id]
        allowed = (
            f"/workspace/evidence/{dispatch.dispatch_id}/",
            f"/workspace/artifacts/{dispatch.dispatch_id}/",
        )
        if not any(path == p.rstrip("/") or path.startswith(p) for p in allowed):
            raise PermissionError("Material is outside current dispatch snapshot")
        target = reject_links(
            self.report_root(context) / path.removeprefix("/workspace/")
        )
        files = list(tree_files(target)) if target.is_dir() else [target]
        for file in files:
            virtual = (
                "/workspace/" + file.relative_to(self.report_root(context)).as_posix()
            )
            expected = scope["paths"].get(virtual)
            if not expected:
                raise PermissionError("Unregistered material")
            if hashlib.sha256(file.read_bytes()).hexdigest() != expected:
                raise ValueError("Material hash mismatch")

    async def acquire(
        self,
        context,
        tool_call_id,
        server,
        tool_name,
        arguments,
        fetch,
        *,
        kind="financial_data",
        parent_id=None,
        schema_hash=None,
        normalize=None,
    ):
        dispatch, _ = await self.scope(context, "searcher")
        key = digest([dispatch.dispatch_id, tool_call_id, server, tool_name, arguments])
        query_key = digest([dispatch.dispatch_id, server, tool_name, arguments])
        lock = self._acquisition_locks.setdefault(
            (context.report_id, query_key), asyncio.Lock()
        )
        async with lock:
            state = await self.repository.read_control(context)
            if failure := state.role_data.get("source_failures", {}).get(query_key):
                return failure
            attempt = state.role_data.get("acquisitions", {}).get(key)
            if attempt and attempt.get("error"):
                return attempt["error"]
            if attempt is None:
                # Same dispatch + canonical query reuses a completed response,
                # including empty results; completed-only, never pending/unknown.
                cached = state.role_data.get("source_results", {}).get(query_key)
                if cached and "raw" in cached:
                    attempt = dict(cached)
            if attempt is None or "raw" not in attempt:
                journal = reject_links(
                    self.report_root(context) / "artifacts" / "acquisitions"
                )
                recovered = (
                    list(journal.glob(key + "-*.json")) if journal.exists() else []
                )
                if len(recovered) > 1:
                    raise ValueError("Ambiguous raw acquisition journal")
                if recovered:
                    attempt = json.loads(reject_links(recovered[0]).read_bytes())
                    if recovered[0].stem != key + "-" + digest(attempt):
                        raise ValueError("Acquisition journal hash mismatch")
                elif attempt is not None:
                    raise RuntimeError(
                        "Source request completion is unknown; explicit diagnosis required"
                    )
                else:
                    await self.repository.mutate_control(
                        context,
                        lambda s: s.role_data.setdefault(
                            "acquisitions", {}
                        ).__setitem__(key, {"status": "started"}),
                    )
                    immutable_write(
                        journal / f"{key}.pending",
                        b"unknown completion until response is preserved",
                    )
                # Do not catch permission/cancellation/budget/interrupt exceptions.
                from research_copilot.sources.mcp_client import (
                    MCPProtocolError,
                    MCPTimeout,
                    MCPUnavailable,
                )
                from research_copilot.subagents.searcher import SourceUnavailable

                if not recovered:
                    try:
                        raw = await fetch(arguments)
                    except (
                        MCPUnavailable,
                        MCPTimeout,
                        MCPProtocolError,
                        SourceUnavailable,
                    ) as exc:
                        failure = {
                            "ok": False,
                            "error": {
                                "code": getattr(exc, "code", None)
                                or "source_unavailable",
                                "source": server,
                                # 异常文本可能携带凭证等敏感信息，一律不回显。
                                "message": (
                                    "Source request failed; identical failed query "
                                    "will not be repeated"
                                ),
                                "retryable": False,
                            },
                        }
                        await self.repository.mutate_control(
                            context,
                            lambda s: s.role_data.setdefault(
                                "source_failures", {}
                            ).__setitem__(query_key, failure),
                        )
                        return failure
                    attempt = {
                        "raw": raw,
                        "collected_at": datetime.now(timezone.utc).isoformat(),
                        "server": server,
                        "tool": tool_name,
                        "arguments": arguments,
                        "schema_hash": schema_hash,
                        "kind": kind,
                        "parent_id": parent_id,
                    }
                    immutable_write(
                        journal / f"{key}-{digest(attempt)}.json",
                        canonical(attempt).encode(),
                    )

                def remember(state):
                    state.role_data.setdefault("acquisitions", {})[key] = attempt
                    state.role_data.setdefault("source_results", {})[query_key] = {
                        field: attempt[field]
                        for field in (
                            "raw",
                            "collected_at",
                            "server",
                            "tool",
                            "arguments",
                            "schema_hash",
                            "kind",
                            "parent_id",
                        )
                    }

                await self.repository.mutate_control(context, remember)

            outcome = None
            if normalize is not None:
                cached_state = await self.repository.read_control(context)
                brief = (
                    cached_state.role_data["scopes"][dispatch.dispatch_id].get(
                        "task_brief"
                    )
                    or {}
                )
                outcome = normalize(attempt["raw"], brief)
            response_hash = digest(attempt["raw"])

            def register(state):
                data = state.role_data
                scope = data["scopes"][dispatch.dispatch_id]
                drafts = (
                    outcome.drafts
                    if outcome is not None
                    else (None,)  # legacy single-evidence path below
                )
                receipts = []
                added = []
                for draft in drafts:
                    ident, content_hash = register_original(
                        state,
                        server=server,
                        tool_name=tool_name,
                        arguments=arguments,
                        raw=attempt["raw"] if draft is None else draft.raw,
                        collected_at=attempt["collected_at"],
                        kind=kind if draft is None else draft.kind,
                        parent_id=parent_id,
                        schema_hash=schema_hash,
                        identity_hash=None if draft is None else draft.identity,
                        provenance=(
                            None
                            if draft is None
                            else {
                                "response_hash": response_hash,
                                "item_pointer": draft.pointer,
                            }
                        ),
                    )
                    if ident not in scope["evidence_ids"]:
                        scope["evidence_ids"].append(ident)
                        added.append(ident)
                    receipts.append(
                        {
                            "evidence_id": ident,
                            "content_hash": content_hash,
                            "path": f"/workspace/evidence/{dispatch.dispatch_id}/{ident}.json",
                            "segments_path": f"/workspace/evidence/{dispatch.dispatch_id}/{ident}.segments.jsonl",
                            "kind": kind if draft is None else draft.kind,
                        }
                    )
                stored = state.role_data.setdefault("acquisitions", {}).setdefault(
                    key, attempt
                )
                if "registered_evidence" in stored:
                    # Same tool-call replay returns the original completed receipt.
                    added = list(stored["registered_evidence"])
                else:
                    stored["registered_evidence"] = list(added)
                return receipts, added

            receipts, added = await self.repository.mutate_control(context, register)
            await self.project(context, dispatch)
            if outcome is not None:
                return {
                    "ok": True,
                    "evidence": receipts,
                    "new_evidence_ids": added,
                    "receipt_id": key,
                    "response_hash": response_hash,
                    "pagination": outcome.pagination,
                    "warnings": list(outcome.warnings),
                }
            (receipt,) = receipts
            return {
                "ok": True,
                "evidence_id": receipt["evidence_id"],
                "content_hash": receipt["content_hash"],
                "path": receipt["path"],
                "segments_path": receipt["segments_path"],
                "kind": receipt["kind"],
                "new_evidence_ids": added,
                "receipt_id": key,
            }

    async def map_result(self, context, dispatch, value, *, validate_only=False):
        from research_copilot.subagents.contracts import (
            RESULT_TYPES,
            BusinessInputError,
        )

        result = RESULT_TYPES[dispatch.role].model_validate(value)
        state = await self.repository.read_control(context)
        scope = state.role_data["scopes"][dispatch.dispatch_id]
        recalled_experiences = {
            item["experience_id"]
            for item in scope.get("experiences", [])
            if isinstance(item, dict) and item.get("experience_id")
        }
        used_experiences = list(result.used_experience_ids)
        unknown_experiences = set(used_experiences) - recalled_experiences
        if unknown_experiences:
            raise BusinessInputError(
                "结果引用了本次未召回的经验："
                + ", ".join(sorted(unknown_experiences)[:5])
            )
        if scope.get("task_brief") and result.task_id != scope["task_brief"]["task_id"]:
            # 明确指出应有的 task_id：只报“不一致”会让子代理在证据耗尽后
            # 反复翻文件猜测（WLY-20261108 第十一轮 analysist 修复失败）。
            raise BusinessInputError(
                "Result task_id differs from the frozen TaskBrief: expected "
                + str(scope["task_brief"]["task_id"])
                + ", received "
                + str(result.task_id)
            )
        if not set(result.evidence_ids) <= set(scope["evidence_ids"]):
            unauthorized = sorted(set(result.evidence_ids) - set(scope["evidence_ids"]))
            raise BusinessInputError(
                "Result declares evidence outside this dispatch: "
                + ", ".join(unauthorized[:8])
                + ". Use only evidence IDs from the authorized index."
            )
        facts = []
        if dispatch.role == "searcher":
            from research_copilot.subagents.facts import validate_facts

            facts = validate_facts(result, state, scope)
        for ref in result.artifact_refs:
            obj = state.role_data.get("objects", {}).get(ref.artifact_id)
            if not obj:
                await self.repository.mutate_control(
                    context,
                    lambda s: s.role_data.setdefault(
                        "rejected_task_results", {}
                    ).setdefault(
                        dispatch.dispatch_id,
                        {
                            "error": "invalid_submission_receipt",
                            "result": result.model_dump(),
                        },
                    ),
                )
                raise MissingSubmissionReceipt(
                    "Result lacks a valid submission receipt"
                )
            if (
                obj["ref"] != ref.model_dump()
                or obj.get("dispatch_id") != dispatch.dispatch_id
            ):
                same_dispatch = obj.get("dispatch_id") == dispatch.dispatch_id
                if not same_dispatch and result.status == "completed":
                    # 完成态引用其他派单的回执属越权伪造，保持不可修复。
                    raise ValueError("Result lacks a valid submission receipt")
                if not same_dispatch:
                    # 未完成结果（partial/blocked）不应引用其他派单的产物：可修复，
                    # 由角色去掉 artifact_refs 后重交（WLY-20261108 第十一轮
                    # chart-only 分析引用主分析产物导致整轮崩溃）。
                    raise MissingSubmissionReceipt(
                        "Non-completed result cannot cite another dispatch's artifact; "
                        "omit artifact_refs or cite only this dispatch's submissions"
                    )
                # 同一派单内回执内容不符（续跑后引用了旧版本/旧哈希）可修复：
                # 交给角色按服务端回执重写，而不是终止整轮。
                raise MissingSubmissionReceipt(
                    "Result lacks a valid submission receipt"
                )
        if dispatch.role == "analysist" and result.status == "completed":
            if not result.analysis_id or not any(
                ref.artifact_id == result.analysis_id for ref in result.artifact_refs
            ):
                raise ValueError(
                    "Completed analysis_id differs from submission receipt"
                )
            obj = state.role_data["objects"][result.analysis_id]
            if (
                obj["kind"] != "analysis"
                or state.artifacts.get("analysis")
                != ArtifactRef.model_validate(obj["ref"])
                or not set(result.metric_ids) <= set(obj["payload"]["metric_ids"])
                or not set(result.chart_ids) <= set(obj["payload"]["chart_ids"])
            ):
                raise ValueError(
                    "Analysis result cites unsubmitted metric/chart receipts"
                )
        if dispatch.role == "writer" and result.status == "completed":
            obj = state.role_data.get("objects", {}).get(result.draft_id)
            if (
                not obj
                or obj["kind"] != "report"
                or state.artifacts.get("report")
                != ArtifactRef.model_validate(obj["ref"])
                or obj["ref"]["version"] != str(result.draft_version)
                or result.report_type != obj["payload"]["report_type"]
                or result.analysis_ids != obj["payload"]["analysis_ids"]
                or not any(
                    ref.artifact_id == result.draft_id for ref in result.artifact_refs
                )
            ):
                raise ValueError("Writing result differs from submitted draft receipt")
        if result.status != "completed" and not result.gaps:
            raise BusinessInputError("Incomplete result must describe gaps")
        if (
            dispatch.role != "searcher"
            and result.status == "completed"
            and not result.artifact_refs
        ):
            raise BusinessInputError("Completed result requires a submitted artifact")
        effects = []
        if not validate_only:

            def save_result(s):
                from research_copilot.runtime.rework import validate_dispatch

                validate_dispatch(s, s.dispatches[dispatch.dispatch_id])
                data = s.role_data
                data.setdefault("task_results", {})[dispatch.dispatch_id] = (
                    result.model_dump()
                )
                from research_copilot.runtime.autonomy import autonomous

                if (
                    autonomous(s)
                    and dispatch.role == "searcher"
                    and result.status != "completed"
                    and "truncated_search_result" in result.source_warnings
                ):
                    data.setdefault("dispatch_failures", {})[dispatch.dispatch_id] = {
                        "code": "truncated_search_result",
                        "repairable": True,
                    }
                if used_experiences:
                    effects.append(
                        {
                            "kind": "experience_adopted",
                            "report_id": context.report_id,
                            "dispatch_id": dispatch.dispatch_id,
                            "experience_ids": used_experiences,
                            "adoption_refs": {
                                "task_id": result.task_id,
                                "status": result.status,
                            },
                        }
                    )
                if facts:
                    brief = data["scopes"][dispatch.dispatch_id].get("task_brief") or {}
                    keys = {fact["input_ref"]["key"] for fact in facts}
                    sheet = {
                        "facts": facts,
                        "evidence_ids": result.evidence_ids,
                        "plan": s.dispatches[dispatch.dispatch_id]["inputs"].get(
                            "plan"
                        ),
                        "dispatch_id": dispatch.dispatch_id,
                        "source_warnings": result.source_warnings,
                        "missing_required": sorted(
                            set(brief.get("required_fields", [])) - keys
                        ),
                        "missing_optional": sorted(
                            set(brief.get("optional_fields", [])) - keys
                        ),
                        "gaps": [gap.model_dump() for gap in result.gaps],
                    }
                    ident = "facts-" + digest(sheet)
                    data.setdefault("fact_sheets", {})[ident] = sheet
                    ids = data["scopes"][dispatch.dispatch_id].setdefault(
                        "fact_sheet_ids", []
                    )
                    if ident not in ids:
                        ids.append(ident)

            if self.experience is not None:
                await self.repository.mutate_control(
                    context, save_result, effects=effects
                )
            else:
                await self.repository.mutate_control(context, save_result)
            if facts:
                await self.project(context, dispatch)
        status = "completed" if result.status == "completed" else "needs_input"
        if dispatch.role == "searcher" and result.status == "partial":
            # Optional gaps do not block downstream roles; the fact sheet still
            # records them, and the report must state the limitation.
            brief = scope.get("task_brief") or {}
            required = set(brief.get("required_fields", []))
            provided = {fact["input_ref"]["key"] for fact in facts}
            missing = required - provided
            if required and (
                not missing
                or all(derives_from_provided(key, provided) for key in missing)
            ):
                status = "completed"
        return RoleResult(
            role=dispatch.role,
            status=status,
            summary=result.summary
            + (
                f"\n事实清单 {len(facts)} 项：/workspace/artifacts/{dispatch.dispatch_id}/fact_index.json"
                if facts
                else ""
            ),
            references=[
                *result.artifact_refs,
                *(
                    ArtifactRef(
                        artifact_id=ident,
                        version="1",
                        content_hash=state.role_data["evidence"][ident]["content_hash"],
                    )
                    for ident in result.evidence_ids
                ),
            ],
            gaps=[f"{g.description}: {g.impact}" for g in result.gaps],
            used_experience_ids=used_experiences,
        )
