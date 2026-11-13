"""Frozen review material snapshots and mandatory read coverage."""

import hashlib
import json
import re

from research_copilot.contracts import ArtifactRef
from research_copilot.subagents.services import canonical, digest, immutable_write
from research_copilot.workspace import bundled_resources
from research_copilot.workspace.filesystem import reject_links

TARGET_TOOLS = {
    "document": "read_review_document",
    "evidence": "read_snapshot_evidence",
    "analysis": "read_snapshot_analysis",
    "metric": "read_snapshot_metric",
    "chart": "read_snapshot_analysis",
    "template": "read_snapshot_template",
    "rules": "read_snapshot_rules",
    "execution": "read_snapshot_execution",
    "code": "read_snapshot_execution",
    "chart_image": "read_snapshot_chart",
}


class SnapshotService:
    def __init__(self, services, *, resources_root=None):
        self.services = services
        self.resources_root = resources_root or bundled_resources()

    INLINE_MAX_BYTES = 98304

    def _inline_delivered(self, content, analysis_payload, branch):
        """Per-target delivery text for one review branch, or None when oversized.

        Metric receipts are delivered as compact rows (registration already
        verified lineage and re-computed values); quotes are validated against
        the delivered text, not the raw receipt JSON.
        """
        metric_ids = list(analysis_payload.get("metric_ids", []))
        # Target indices count chunks, not receipts. Only a one-to-one layout
        # can substitute a compact receipt for its frozen text.
        compact_metrics = sum(
            t["kind"] == "metric" for t in content["targets"].values()
        ) == len(metric_ids)
        delivered, total = {}, 0
        for target_id in content[branch + "_targets"]:
            target = content["targets"][target_id]
            if target["kind"] == "metric" and compact_metrics:
                index = int(target_id.split(":")[1])
                receipt = content["validation"]["metric_receipts"].get(
                    metric_ids[index], {}
                )
                text = canonical(
                    {
                        "target_id": target_id,
                        "metric_id": receipt.get("metric_id"),
                        "key": receipt.get("key") or receipt.get("local_key"),
                        "value": receipt.get("value"),
                        "unit": receipt.get("unit"),
                        "display_precision": receipt.get("display_precision"),
                        "is_estimate": receipt.get("is_estimate"),
                        "semantic_type": receipt.get("semantic_type"),
                        "formula_id": receipt.get("formula_id"),
                        "numerator_basis": receipt.get("numerator_basis"),
                        "denominator_basis": receipt.get("denominator_basis"),
                        "input_keys": receipt.get("input_keys", []),
                        "evidence_ids": receipt.get("evidence_ids", []),
                        "description": str(receipt.get("description", ""))[:400],
                    }
                )
            else:
                text = target["text"]
            delivered[target_id] = text
            total += len(text.encode("utf-8"))
            if total > self.INLINE_MAX_BYTES:
                return None
        return delivered

    async def prepare_snapshot(self, context):
        dispatch, state = await self.services.scope(context, "reviewer")
        existing = state.role_data.get("review_dispatches", {}).get(
            dispatch.dispatch_id
        )
        if existing:
            return existing
        ref = ArtifactRef.model_validate(
            state.dispatches[dispatch.dispatch_id]["inputs"]["report"]
        )
        draft = await self.services.get_object(context, ref.artifact_id, kind="report")
        if draft["ref"] != ref.model_dump():
            raise ValueError("Review draft differs from authorized version")
        payload = draft["payload"]
        analysis = await self.services.get_object(
            context, payload["analysis_ids"][0], kind="analysis"
        )
        sections = re.split(r"(?=^## )", payload["document"], flags=re.M)
        if len(sections) > 1:
            sections[1] = sections[0] + sections[1]
            sections = sections[1:]
        materials = [
            *(("document", section) for section in sections),
            ("rules", canonical(payload["rules"])),
        ]
        prior_issues = list(state.role_data.get("regression_issues", []))
        if rework := state.artifacts.get("rework"):
            rework_payload = state.role_data["rework"][rework.artifact_id]
            prior_review = state.role_data.get("reviews", {}).get(
                rework_payload.get("review_id"), {}
            )
            prior_issues.extend(prior_review.get("issues", []))
        prior_issues = list({i["issue_id"]: i for i in prior_issues}.values())
        if prior_issues:
            materials.append(("analysis", canonical({"issues_to_close": prior_issues})))
        evidence_ids = list(
            dict.fromkeys(
                [*payload["evidence_ids"], *analysis["payload"]["evidence_ids"]]
            )
        )
        evidence_hashes = {}
        for ident in evidence_ids:
            record = state.role_data["evidence"][ident]
            if digest(record["raw"]) != record["content_hash"]:
                raise ValueError("Evidence hash mismatch")
            evidence_hashes[ident] = record["content_hash"]
            proof = state.role_data.get("verified_publications", {}).get(ident)
            material = (
                {**record, "verified_publication": proof}
                if proof and proof.get("content_hash") == record["content_hash"]
                else record
            )
            materials.append(("evidence", canonical(material)))
        if analysis["ref"] != payload["analysis_ref"]:
            raise ValueError("Analysis snapshot hash mismatch")
        materials.append(("analysis", canonical(analysis["payload"])))
        metric_receipts = {}
        from research_copilot.subagents.analysis_recovery import validate_receipt_origin

        for ident in analysis["payload"]["metric_ids"]:
            metric_receipts[ident] = state.role_data["metrics"][
                analysis["dispatch_id"]
            ][ident]
            if metric_receipts[ident].get("execution_id"):
                validate_receipt_origin(
                    state, analysis["dispatch_id"], ident, "metrics"
                )
            materials.append(
                (
                    "metric",
                    canonical(
                        state.role_data["metrics"][analysis["dispatch_id"]][ident]
                    ),
                )
            )
        execution_ids = {
            m["execution_id"] for m in metric_receipts.values() if m.get("execution_id")
        }
        chart_images = []
        for ident in analysis["payload"]["chart_ids"]:
            chart = state.role_data["charts"][analysis["dispatch_id"]][ident]
            if chart["receipt"].get("execution_id"):
                validate_receipt_origin(state, analysis["dispatch_id"], ident, "charts")
            for path, expected in chart["files"].items():
                if (
                    hashlib.sha256(
                        reject_links(
                            self.services.report_root(context) / path
                        ).read_bytes()
                    ).hexdigest()
                    != expected
                ):
                    raise ValueError("Chart snapshot hash mismatch")
            materials.append(("chart", canonical(chart)))
            if execution_id := chart["receipt"].get("execution_id"):
                execution_ids.add(execution_id)
                for path in chart["files"]:
                    source = reject_links(self.services.report_root(context) / path)
                    if source.suffix == ".json":
                        materials.append(
                            (
                                "chart",
                                canonical(
                                    {
                                        "path": path,
                                        "content": json.loads(source.read_bytes()),
                                    }
                                ),
                            )
                        )
                    elif source.suffix == ".png":
                        import base64

                        raw = source.read_bytes()
                        chart_images.append(
                            (ident, raw, base64.b64encode(raw).decode("ascii"))
                        )
        execution_receipts = {}
        from research_copilot.sandbox.receipts import verify_execution

        for ident in sorted(execution_ids):
            execution = state.role_data.get("executions", {}).get(ident)
            if not execution:
                raise ValueError("Missing authorized execution receipt")
            verify_execution(self.services.report_root(context), execution)
            execution_receipts[ident] = execution
            materials.append(("execution", canonical(execution)))
            for path in execution["code_paths"]:
                source = reject_links(self.services.report_root(context) / path)
                try:
                    code = source.read_text("utf-8")
                except UnicodeDecodeError:
                    code = "Binary input, sha256=" + execution["files"][path]
                materials.append(("code", canonical({"path": path, "content": code})))
        template = (
            self.resources_root
            / f"skills/writer/reports/references/{payload['report_type']}.md"
        ).read_text("utf-8")
        if hashlib.sha256(template.encode()).hexdigest() != payload["template_hash"]:
            raise ValueError("Adopted template hash changed")
        materials.append(("template", template))
        targets, counts = {}, {}
        for kind, text in materials:
            for start in range(0, len(text), 3000):
                index = counts.get(kind, 0)
                counts[kind] = index + 1
                target_id = f"{kind}:{index}"
                targets[target_id] = {
                    "kind": kind,
                    "text": text[start : start + 3000],
                    "hash": hashlib.sha256(
                        text[start : start + 3000].encode()
                    ).hexdigest(),
                    "tool": TARGET_TOOLS[kind],
                }
        for ident, raw, encoded in chart_images:
            target_id = f"chart_image:{len([t for t in targets if t.startswith('chart_image:')])}"
            description = (
                "Frozen static chart "
                + ident
                + "; sha256="
                + hashlib.sha256(raw).hexdigest()
            )
            targets[target_id] = {
                "kind": "chart_image",
                "text": description,
                "hash": hashlib.sha256(description.encode()).hexdigest(),
                "tool": TARGET_TOOLS["chart_image"],
                "image_base64": encoded,
                "image_hash": hashlib.sha256(raw).hexdigest(),
            }
        snapshot_id = (
            "snapshot-"
            + digest([context.report_id, dispatch.dispatch_id, ref.model_dump()])[:24]
        )
        content = {
            "snapshot_id": snapshot_id,
            "review_session_id": "review-" + dispatch.dispatch_id,
            "reviewed": ref.model_dump(),
            "policy_version": context.policy_version,
            "threshold": 0.85,
            "prior_issues": prior_issues,
            "execution_mode": (
                state.role_data.get("runtime_manifest", {}).get("preflight") or {}
            ).get("mode", "production"),
            "document": payload["document"],
            "rules": payload["rules"],
            "targets": targets,
            "validation": {
                "declared_evidence_ids": payload["evidence_ids"],
                "evidence_hashes": evidence_hashes,
                "required_headings": re.findall(r"^## (.+)$", template, re.M),
                "body_hash": payload["body_hash"],
                "metric_receipts": metric_receipts,
                "calculation_receipts": state.role_data.get("metrics", {}).get(
                    analysis["dispatch_id"], {}
                ),
                "execution_receipts": execution_receipts,
                "claims": payload.get("claims"),
                "analysis_claims": analysis["payload"].get("claims", []),
                "verified_facts": analysis["payload"].get("verified_facts", []),
            },
            "quality_targets": [
                ident for ident, target in targets.items() if target["kind"] != "rules"
            ],
            "compliance_targets": [
                ident
                for ident, target in targets.items()
                if target["kind"] in {"document", "rules"}
            ],
        }
        inline_delivery = {}
        for branch in ("quality", "compliance"):
            delivered = self._inline_delivered(content, analysis["payload"], branch)
            if delivered is not None:
                inline_delivery[branch] = {"targets": delivered}
        if inline_delivery:
            content["inline_delivery"] = inline_delivery
        manifest_path = f"/workspace/artifacts/reviews/{snapshot_id}.json"
        immutable_write(
            self.services.report_root(context)
            / manifest_path.removeprefix("/workspace/"),
            canonical(content).encode(),
        )
        snapshot = {
            "snapshot_id": snapshot_id,
            "review_session_id": content["review_session_id"],
            "reviewed": ref.model_dump(),
            "content_hash": digest(content),
            "manifest_path": manifest_path,
            "policy_version": context.policy_version,
        }

        def save(state):
            state.role_data.setdefault("review_dispatches", {})[
                dispatch.dispatch_id
            ] = snapshot
            state.role_data.setdefault("review_snapshots", {})[snapshot_id] = snapshot

        await self.services.repository.mutate_control(context, save)
        return snapshot

    async def load_snapshot(self, context, snapshot):
        dispatch, state = await self.services.scope(context, "reviewer")
        expected = state.role_data.get("review_dispatches", {}).get(
            dispatch.dispatch_id
        )
        if snapshot != expected or snapshot["policy_version"] != context.policy_version:
            raise PermissionError("Review snapshot identity changed")
        path = reject_links(
            self.services.report_root(context)
            / snapshot["manifest_path"].removeprefix("/workspace/")
        )
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != snapshot["content_hash"]:
            raise ValueError("Review snapshot hash mismatch")
        return json.loads(raw)

    async def read_target(self, context, snapshot, branch, tool, target_id):
        content = await self.load_snapshot(context, snapshot)
        required = content[branch + "_targets"]
        target = content["targets"].get(target_id)
        if target_id not in required or not target or target["tool"] != tool:
            raise PermissionError("Target is outside the branch read-only snapshot")
        if hashlib.sha256(target["text"].encode()).hexdigest() != target["hash"]:
            raise ValueError("Review target hash mismatch")

        def record(state):
            reads = (
                state.role_data.setdefault("review_reads", {})
                .setdefault(snapshot["snapshot_id"], {})
                .setdefault(branch, [])
            )
            if target_id not in reads:
                reads.append(target_id)

        await self.services.repository.mutate_control(context, record)
        result = {
            "target_id": target_id,
            "content": target["text"],
            "content_hash": target["hash"],
            "complete": True,
        }
        if "image_base64" in target:
            import base64

            if (
                hashlib.sha256(base64.b64decode(target["image_base64"])).hexdigest()
                != target["image_hash"]
            ):
                raise ValueError("Frozen chart image hash mismatch")
            return [
                {"type": "text", "text": canonical(result)},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64," + target["image_base64"]
                    },
                },
            ]
        return result

    async def read_targets(self, context, snapshot, branch, target_ids, cursor=None):
        from research_copilot.subagents.review_batches import read_batch

        return await read_batch(self, context, snapshot, branch, target_ids, cursor)

    async def precheck(self, context, snapshot):
        content = await self.load_snapshot(context, snapshot)
        issues = []

        def issue(category, reason, *, hard=False, quote=""):
            target = next(
                (
                    ident
                    for ident, value in content["targets"].items()
                    if value["kind"] == "document" and quote in value["text"]
                ),
                "document:0",
            )
            issues.append(
                {
                    "category": category,
                    "severity": "major",
                    "target_id": target,
                    "location": f"character offset {content['document'].find(quote)}"
                    if quote
                    else "full document",
                    "quote": quote,
                    "basis": "frozen policy",
                    "reason": reason,
                    "suggestion": "Resolve before approval",
                    "hard": hard,
                    "deterministic": True,
                }
            )

        text = content["document"]
        rules = content["rules"]
        validation = content["validation"]
        cited = set(re.findall(r"\[\^([^\]]+)\]", text))
        declared = set(validation["declared_evidence_ids"])
        definitions = set(re.findall(r"^\[\^(E-[0-9]{6,})\]:", text, re.M))
        if not cited <= declared or definitions != declared or not declared:
            issue(
                "reference",
                "Frozen document citations/definitions do not match registered evidence",
                hard=True,
            )
        first = re.search(r"^\[\^E-[0-9]{6,}\]:", text, re.M)
        body = text[: first.start() - 2] if first and first.start() >= 2 else ""
        if hashlib.sha256(body.encode()).hexdigest() != validation["body_hash"]:
            issue(
                "integrity",
                "Frozen report body hash differs from submitted candidate",
                hard=True,
            )
        from research_copilot.subagents.claim_validation import validate_claims

        numeric_errors = validate_claims(
            body,
            validation.get("claims"),
            validation["metric_receipts"],
            {
                ident: {"content_hash": content_hash}
                for ident, content_hash in validation["evidence_hashes"].items()
            },
            validation.get("analysis_claims", []),
            facts=validation.get("verified_facts", []),
        )
        for error in numeric_errors:
            issue("numeric", error, hard=True)
        from research_copilot.subagents.metric_quality import (
            validate_registered_metrics,
        )

        try:
            validate_registered_metrics(
                validation.get("calculation_receipts", validation["metric_receipts"]),
                validation["metric_receipts"],
            )
        except ValueError as exc:
            issue("metric", "Independent calculation check: " + str(exc), hard=True)
        if re.findall(r"^## (.+)$", body, re.M) != validation["required_headings"]:
            issue(
                "structure",
                "Frozen report body does not cover the adopted template structure",
                hard=True,
            )
        LEGACY_FORMULA_VERSION = "m3-formula-2"

        def invalid_lineage(value):
            if isinstance(value, dict):
                ident = value.get("evidence_id")
                if ident and validation["evidence_hashes"].get(ident) != value.get(
                    "content_hash"
                ):
                    return True
                return any(invalid_lineage(item) for item in value.values())
            return isinstance(value, list) and any(
                invalid_lineage(item) for item in value
            )

        for ident, metric in validation["metric_receipts"].items():
            if (
                metric.get("metric_id") != ident
                or (
                    metric.get("formula_version") != LEGACY_FORMULA_VERSION
                    and not (
                        metric.get("formula_version") == "sandbox-code-v1"
                        and metric.get("execution_id")
                        in validation.get("execution_receipts", {})
                        and ident
                        in validation["execution_receipts"][metric["execution_id"]][
                            "response"
                        ]["metric_ids"]
                        and validation["execution_receipts"][metric["execution_id"]][
                            "status"
                        ]
                        == "registered"
                    )
                )
                or not set(metric.get("evidence_ids", []))
                <= validation["evidence_hashes"].keys()
                or invalid_lineage(metric.get("inputs", []))
            ):
                issue(
                    "metric",
                    "Metric receipt identity/formula/evidence lineage is invalid: "
                    + ident,
                    hard=True,
                )
        if rules.get("is_example") and content.get("execution_mode") != "test":
            issue("policy", "Example compliance rules require replacement", hard=True)
        for name in rules.get("required_disclosures", []):
            key = {
                "risk_warning": "risk_warning_template",
                "disclaimer": "disclaimer_template",
            }.get(name, name)
            required = rules.get(key)
            if not required or required not in text:
                issue("disclosure", f"Missing required disclosure: {name}", hard=True)
        for value in rules.get("sensitive_words", []):
            if value in text:
                issue(
                    "compliance",
                    "Sensitive term requires contextual human adjudication",
                    quote=value,
                )
        # Use the existing bounded subprocess scanner for administrator supplied regex.
        from research_copilot.review.scan import scan_rules_subprocess
        import asyncio

        hits = await asyncio.to_thread(
            scan_rules_subprocess,
            {
                "text": text,
                "rules": {**rules, "sensitive_words": [], "required_disclosures": []},
            },
        )
        for hit in hits:
            issue(
                "compliance",
                "Prohibited pattern requires human adjudication",
                quote=hit.fragment,
            )
        return {"checks_complete": True, "issues": issues}
