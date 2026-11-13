"""Full-version Markdown validation and immutable draft submission."""

import hashlib
import json
import re

from langchain.tools import ToolRuntime, tool
from research_copilot.contracts import RunContext
from research_copilot.subagents.services import digest
from research_copilot.runtime.autonomy import autonomous, report_policy, analysis_input
from research_copilot.subagents.contracts import BusinessInputError
from research_copilot.workspace import bundled_resources, REPORT_TYPES
from research_copilot.workspace.filesystem import reject_links

EVIDENCE_CITATION = re.compile(r"\[\^(E-[0-9]{6,})\]")
NUMBER_TOKEN = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")


def token_set(text):
    values = set()
    for match in NUMBER_TOKEN.finditer(text):
        value = match.group().replace(",", "").removeprefix("+")
        values.add(value)
        values.add(value.lstrip("-"))
    return values


def _footnote_text(value, limit=300):
    """External source text is data: keep one line, neutralise brackets, bound length."""

    text = " ".join(str(value or "").split())
    return text.replace("[", "\\[").replace("]", "\\]")[:limit]


class WritingService:
    def __init__(self, services, resources_root=None):
        self.services = services
        self.resources_root = resources_root or bundled_resources()

    async def validate(
        self, context, *, candidate_path, report_type, analysis_ids, claims_path=None
    ):
        dispatch, state = await self.services.scope(context, "writer")
        plans = [report_policy(state)] if report_policy(state) else []
        if (
            len(plans) != 1
            or report_type not in REPORT_TYPES
            or plans[0]["report_type"] != report_type
        ):
            return {
                "ok": False,
                "errors": ["Report type conflicts with the confirmed plan"],
            }
        approved = analysis_input(state)
        if not approved or analysis_ids != [approved.artifact_id]:
            raise PermissionError("Writer requires the current confirmed analysis")
        analysis = await self.services.get_object(
            context, approved.artifact_id, kind="analysis"
        )
        body = await self.services.candidate(context, candidate_path, "writer")
        template_path = f"skills/writer/reports/references/{report_type}.md"
        from research_copilot.workspace import validate_resources

        validate_resources(self.resources_root)
        template = (self.resources_root / template_path).read_text("utf-8")

        claim_manifest = (
            json.loads(await self.services.candidate(context, claims_path, "writer"))
            if claims_path
            else {"body_hash": hashlib.sha256(body.encode()).hexdigest(), "claims": []}
        )
        if isinstance(claim_manifest, dict) and "sections" in claim_manifest:
            from pydantic import ValidationError
            from research_copilot.subagents.contracts import (
                ReportFileClaims,
                ReportSection,
            )

            try:
                declared = ReportFileClaims.model_validate(claim_manifest)
                split = re.split(r"^## (.+)\s*$", body, flags=re.M)
                texts = dict(zip(split[1::2], split[2::2]))
                names = [section.heading for section in declared.sections]
                if len(set(names)) != len(names) or not set(names) <= texts.keys():
                    raise BusinessInputError(
                        "claims.sections headings must uniquely match draft headings"
                    )
                sections = [
                    ReportSection(**section.model_dump(), text=texts[section.heading])
                    for section in declared.sections
                ]
                metrics = state.role_data.get("metrics", {}).get(
                    analysis["dispatch_id"], {}
                )
                claim_manifest = self._build_claim_manifest(body, sections, metrics)
            except ValidationError as exc:
                return {
                    "ok": False,
                    "errors": [
                        ".".join(map(str, error["loc"])) + ": " + error["msg"]
                        for error in exc.errors(include_input=False)
                    ],
                }
            except BusinessInputError as exc:
                return {"ok": False, "errors": [str(exc)]}
        return await self._check_body(
            context,
            dispatch=dispatch,
            state=state,
            analysis=analysis,
            template=template,
            body=body,
            claim_manifest=claim_manifest,
            report_type=report_type,
            analysis_ids=analysis_ids,
        )

    async def _check_body(
        self,
        context,
        *,
        dispatch,
        state,
        analysis,
        template,
        body,
        claim_manifest,
        report_type,
        analysis_ids,
    ):
        errors = []
        from research_copilot.subagents.quality import validate_claims, assess_source

        metric_store = state.role_data.get("metrics", {}).get(
            analysis["dispatch_id"], {}
        )
        selected_metrics = {
            k: metric_store[k] for k in analysis["payload"]["metric_ids"]
        }
        selected_evidence = {
            k: state.role_data["evidence"][k]
            for k in analysis["payload"]["evidence_ids"]
        }
        errors.extend(
            validate_claims(
                body,
                claim_manifest,
                selected_metrics,
                selected_evidence,
                analysis["payload"].get("claims", []),
                facts=analysis["payload"].get("verified_facts", []),
                required_fields=(
                    state.role_data["scopes"][dispatch.dispatch_id]
                    .get("task_brief", {})
                    .get("required_fields", [])
                    if autonomous(state)
                    else ()
                ),
            )
        )
        headings = re.findall(r"^## (.+)$", template, re.M)
        sections = re.split(r"^## (.+)\s*$", body, flags=re.M)
        actual = sections[1::2]
        if actual != headings:
            errors.append(
                "Required section headings/order do not match the adopted template"
            )
        lengths = re.findall(r"<!-- length: (\d+)-(\d+) 字 -->", template)
        for heading, text, bounds in zip(actual, sections[2::2], lengths):
            count = len(re.sub(r"\s|\[\^E-\d+\]", "", text))
            low, high = map(int, bounds)
            # The template bound is an editorial target; a bounded overage keeps
            # a well-formed section from failing on a few characters.
            if not low <= count <= int(high * 1.35):
                errors.append(
                    f"Section {heading} length {count} is outside {low}-{high}"
                )
        if re.search(r"^\[\^.+?\]:", body, re.M):
            errors.append("Source footnote definitions are generated by the service")
        if re.search(r"\[\^(?!E-[0-9]{6,}\])", body):
            errors.append("Invalid evidence citation syntax")
        evidence_ids = list(dict.fromkeys(EVIDENCE_CITATION.findall(body)))
        grants = state.role_data["scopes"][dispatch.dispatch_id]["evidence_ids"]
        if not evidence_ids:
            errors.append("Report requires evidence citations")
        for ident in evidence_ids:
            if ident not in grants or ident not in analysis["payload"]["evidence_ids"]:
                errors.append(f"Unauthorized evidence citation: {ident}")
            elif (
                digest(state.role_data["evidence"][ident]["raw"])
                != state.role_data["evidence"][ident]["content_hash"]
            ):
                raise ValueError("Evidence hash mismatch")
            elif state.role_data.get("research_as_of"):
                eligibility = assess_source(
                    state.role_data["evidence"][ident],
                    state.role_data["research_as_of"],
                    publication=state.role_data.get("verified_publications", {}).get(
                        ident
                    ),
                )
                if not eligibility["eligible"]:
                    errors.append(
                        f"Ineligible evidence {ident}: {eligibility['reason']}"
                    )
        chart_ids = re.findall(r"!\[[^\]]*\]\(chart:(C-[0-9a-f]{16})\)", body)
        for ident in chart_ids:
            if ident not in analysis["payload"]["chart_ids"]:
                errors.append(f"Unauthorized chart: {ident}")
            else:
                await self.get_artifact(context, ident)
        if re.search(r"!\[[^\]]*\]\((?!chart:C-)[^)]+\)", body):
            errors.append("Report images require authorized chart references")
        return {
            "ok": not errors,
            "errors": errors,
            "evidence_ids": evidence_ids,
            "chart_ids": chart_ids,
            "report_type": report_type,
            "analysis_ids": analysis_ids,
            "claims": claim_manifest,
            "template_hash": hashlib.sha256(template.encode()).hexdigest(),
            "body": body,
        }

    async def get_artifact(self, context, artifact_id):
        dispatch, state = await self.services.scope(context)
        approved = analysis_input(state)
        if not approved:
            raise PermissionError("Confirmed analysis is required")
        analysis = await self.services.get_object(
            context, approved.artifact_id, kind="analysis"
        )
        if artifact_id not in analysis["payload"]["chart_ids"]:
            raise BusinessInputError(
                "Chart is outside the approved analysis; this report has no "
                "authorized chart with that ID. Proceed without charts."
            )
        chart = state.role_data["charts"][analysis["dispatch_id"]][artifact_id]
        if execution_id := chart["receipt"].get("execution_id"):
            from research_copilot.sandbox.receipts import verify_execution

            execution = state.role_data.get("executions", {}).get(execution_id)
            if (
                not execution
                or execution["dispatch_id"] != analysis["dispatch_id"]
                or artifact_id not in execution["response"]["chart_ids"]
            ):
                raise ValueError("Invalid chart execution receipt")
            verify_execution(self.services.report_root(context), execution)
        for path, expected in chart["files"].items():
            if (
                hashlib.sha256(
                    reject_links(self.services.report_root(context) / path).read_bytes()
                ).hexdigest()
                != expected
            ):
                raise ValueError("Chart hash mismatch")
        return {
            **chart,
            "projected_files": state.role_data["scopes"][dispatch.dispatch_id]
            .get("chart_projections", {})
            .get(artifact_id, {}),
        }

    async def submit(self, context, tool_call_id, **arguments):
        result = await self.validate(context, **arguments)
        if not result["ok"]:
            raise BusinessInputError("; ".join(result["errors"]))
        _, state = await self.services.scope(context, "writer")
        analysis = await self.services.get_object(
            context, result["analysis_ids"][0], kind="analysis"
        )
        return await self._publish_document(
            context, tool_call_id, result, analysis, state
        )

    def _locate_claim_value(
        self, body, section_text, section_start, metric_ids, metrics
    ):
        from research_copilot.subagents.claim_validation import render_metric

        targets = {
            token
            for mid in metric_ids
            if mid in metrics
            for token in token_set(render_metric(metrics[mid]))
        }
        if not targets:
            return None
        haystack = section_text or body
        for match in NUMBER_TOKEN.finditer(haystack):
            if token_set(match.group()) & targets:
                return section_start + match.start()
        return None

    async def publish_structured(
        self,
        context,
        *,
        title,
        sections,
        report_type,
        analysis_ids,
        submission_identity,
    ):
        """Structured-submission path: sections ARE the draft; the host assembles the body."""
        dispatch, state = await self.services.scope(context, "writer")
        plans = [report_policy(state)] if report_policy(state) else []
        if (
            len(plans) != 1
            or report_type not in REPORT_TYPES
            or plans[0]["report_type"] != report_type
        ):
            raise BusinessInputError("Report type conflicts with the confirmed plan")
        approved = analysis_input(state)
        if not approved or analysis_ids != [approved.artifact_id]:
            raise PermissionError("Writer requires the current confirmed analysis")
        analysis = await self.services.get_object(
            context, approved.artifact_id, kind="analysis"
        )
        template_path = f"skills/writer/reports/references/{report_type}.md"
        from research_copilot.workspace import validate_resources

        validate_resources(self.resources_root)
        template = (self.resources_root / template_path).read_text("utf-8")
        headings = re.findall(r"^## (.+)$", template, re.M)
        if [section.heading for section in sections] != headings:
            raise BusinessInputError(
                "Section headings/order must match the adopted template exactly: "
                + " | ".join(headings)
            )
        parts = []
        if title:
            parts.append(f"# {title}")
        for section in sections:
            parts.append(f"## {section.heading}\n\n{section.text}")
        body = "\n\n".join(parts)
        metrics = state.role_data.get("metrics", {}).get(analysis["dispatch_id"], {})
        claim_manifest = self._build_claim_manifest(body, sections, metrics)
        result = await self._check_body(
            context,
            dispatch=dispatch,
            state=state,
            analysis=analysis,
            template=template,
            body=body,
            claim_manifest=claim_manifest,
            report_type=report_type,
            analysis_ids=analysis_ids,
        )
        if not result["ok"]:
            raise BusinessInputError("; ".join(result["errors"]))
        return await self._publish_document(
            context, submission_identity, result, analysis, state
        )

    def _build_claim_manifest(self, body, sections, metrics):
        from research_copilot.subagents.claim_validation import render_metric

        claims, claim_errors = [], []
        for section in sections:
            section_start = body.find(section.text)
            section_start = section_start if section_start >= 0 else 0
            for claim in section.claims:
                starts, at = [], body.find(claim.quote)
                while at >= 0 and len(starts) <= 20:
                    starts.append(at)
                    at = body.find(claim.quote, at + 1)
                if not starts:
                    # The model often paraphrases or changes punctuation. The
                    # host owns offset location: locate one of the claim's
                    # registered metric values in the section and use the
                    # containing line as the auditable span.
                    located = self._locate_claim_value(
                        body, section.text, section_start, claim.metric_ids, metrics
                    )
                    if located is None:
                        claim_errors.append(
                            "Claim quote must appear in the report body "
                            f"(found 0): {claim.quote[:80]!r}"
                        )
                        continue
                    line_start = body.rfind("\n", 0, located) + 1
                    line_end = body.find("\n", located)
                    quote = body[
                        line_start : line_end if line_end >= 0 else len(body)
                    ].strip()
                    actual = body.find(quote, line_start) if quote else -1
                    if actual < 0:
                        claim_errors.append(
                            f"Claim quote could not be located: {claim.quote[:80]!r}"
                        )
                        continue
                    line_numbers = token_set(quote)
                    line_metrics = [
                        mid
                        for mid, metric in metrics.items()
                        if line_numbers & token_set(render_metric(metric))
                    ]
                    entry = {
                        "quote": quote,
                        "start": actual,
                        "end": actual + len(quote),
                        "metric_ids": list(
                            dict.fromkeys([*claim.metric_ids, *line_metrics])
                        ),
                        "evidence_ids": list(claim.evidence_ids),
                        "fact_refs": [ref.model_dump() for ref in claim.fact_refs],
                    }
                    if claim.conclusion_id:
                        entry["conclusion_id"] = claim.conclusion_id
                    claims.append(entry)
                    continue
                if len(starts) > 20:
                    claim_errors.append(
                        "Claim quote is too ambiguous; quote a longer unique span: "
                        f"{claim.quote[:80]!r}"
                    )
                    continue
                # A repeated phrase still supports the same numbers at every
                # occurrence; register one locator per occurrence so each
                # numeric span in the body is covered by a claim.
                for start in starts:
                    entry = {
                        "quote": claim.quote,
                        "start": start,
                        "end": start + len(claim.quote),
                        "metric_ids": list(claim.metric_ids),
                        "evidence_ids": list(claim.evidence_ids),
                        "fact_refs": [ref.model_dump() for ref in claim.fact_refs],
                    }
                    if claim.conclusion_id:
                        entry["conclusion_id"] = claim.conclusion_id
                    claims.append(entry)
        if claim_errors:
            raise BusinessInputError("; ".join(claim_errors))
        return {
            "body_hash": hashlib.sha256(body.encode()).hexdigest(),
            "claims": claims,
        }

    async def _publish_document(
        self, context, submission_identity, result, analysis, state
    ):
        footnotes = []
        for ident in result["evidence_ids"]:
            record = state.role_data["evidence"][ident]
            raw = record["raw"] if isinstance(record["raw"], dict) else {}
            structured = raw.get("structured")
            structured = structured if isinstance(structured, dict) else {}
            title = _footnote_text(raw.get("title") or record["source"]["server"])
            # Source text is data; normalize it before adding service-owned definitions.
            parts = [
                title,
                "source=" + record["source"]["server"],
            ]
            published = raw.get("published_at")
            if published:
                parts.append("published=" + _footnote_text(published, 64))
            if record.get("kind") == "search_snippet":
                collection = raw.get("source_ref", {}).get("collection")
                if collection:
                    parts.append("collection=" + _footnote_text(collection, 64))
                if structured.get("page") is not None:
                    parts.append("page=" + _footnote_text(structured["page"], 32))
                if structured.get("heading_path"):
                    parts.append(
                        "section=" + _footnote_text(structured["heading_path"])
                    )
                if structured.get("broker"):
                    parts.append("broker=" + _footnote_text(structured["broker"]))
                if structured.get("report_type"):
                    parts.append(
                        "report_type=" + _footnote_text(structured["report_type"])
                    )
            parts.extend(
                [
                    "collected=" + record["collected_at"],
                    "sha256=" + record["content_hash"],
                ]
            )
            footnotes.append("[^" + ident + "]: " + "; ".join(parts))
        from research_copilot.subagents.claim_validation import render_metric

        appendix = ["## 指标与研究限制"]
        appendix.extend(
            "假设：" + s for s in analysis["payload"]["assumptions"] if s.strip()
        )
        appendix.extend(
            "限制：" + s for s in analysis["payload"]["limitations"] if s.strip()
        )
        for ident in analysis["payload"]["metric_ids"]:
            metric = state.role_data["metrics"][analysis["dispatch_id"]][ident]
            appendix.append(
                f"- {metric.get('description') or metric.get('local_key') or '指标'} "
                f"[{metric.get('local_key', 'legacy')} / {ident}]: "
                f"{render_metric(metric)} {metric.get('unit', '')}"
                + ("（推算）" if metric.get("is_estimate") else "")
                + f"；口径：{metric.get('denominator_basis', 'unknown')}"
            )
        if any(
            state.role_data["evidence"][i]
            .get("source", {})
            .get("historical_availability", "unknown")
            == "unknown"
            for i in result["evidence_ids"]
        ):
            appendix.append(
                "历史可得性（PIT）未知；本报告不构成历史可得性已验证的证明。"
            )
        document = result["body"] + "\n\n" + "\n\n".join(footnotes + appendix) + "\n"
        rules = self.services.report_rules
        document += (
            "\n## 风险声明\n\n"
            + rules["risk_warning_template"]
            + "\n\n## 免责声明\n\n"
            + rules["disclaimer_template"]
            + "\n"
        )
        payload = {
            key: val
            for key, val in result.items()
            if key not in {"ok", "errors", "body"}
        }
        payload.update(
            document=document,
            body_hash=hashlib.sha256(result["body"].encode()).hexdigest(),
            analysis_ref=analysis["ref"],
            policy_version=context.policy_version,
            rules=rules,
        )
        return await self.services.publish_object(
            context, submission_identity, "report", payload
        )


def build_writing_tools(services, *, resources_root=None):
    service = WritingService(services, resources_root)

    @tool
    async def compose_report(
        title: str,
        section_texts: list[str],
        analysis_id: str,
        runtime: ToolRuntime[RunContext],
    ) -> dict:
        """Publish using template-ordered texts and explicit {{fact:key}}, {{metric:ID}}, {{conclusion:ID}} references. Host renders numbers, citations, claims and headings. Include every required conclusion token; never write financial numbers manually. Length limits apply AFTER expansion. Returns a formal draft receipt."""
        from research_copilot.subagents.writer_rendering import render_sections

        scope, state = await services.scope(runtime.context, "writer")
        current = analysis_input(state)
        if not current or analysis_id != current.artifact_id:
            raise PermissionError("Current analysis required")
        analysis = await services.get_object(
            runtime.context, analysis_id, kind="analysis"
        )
        report_type = report_policy(state)["report_type"]
        template = (
            service.resources_root
            / f"skills/writer/reports/references/{report_type}.md"
        ).read_text("utf-8")
        candidate = dict(
            title=title,
            section_texts=section_texts,
            analysis_ref=analysis["ref"],
            template_hash=hashlib.sha256(template.encode()).hexdigest(),
        )
        from research_copilot.subagents.services import immutable_write, canonical

        path = (
            f"tasks/{scope.dispatch_id}/writer-candidate-{digest(candidate)[:16]}.json"
        )
        immutable_write(
            services.report_root(runtime.context) / path,
            canonical(candidate).encode("utf-8"),
        )

        def remember(control):
            control.role_data.setdefault("writer_candidates", {})[scope.dispatch_id] = {
                **candidate,
                "path": path,
                "content_hash": digest(candidate),
            }

        await services.repository.mutate_control(runtime.context, remember)
        title, sections = render_sections(
            title,
            section_texts,
            re.findall(r"^## (.+)$", template, re.M),
            analysis["payload"],
            state.role_data.get("metrics", {}).get(analysis["dispatch_id"], {}),
        )
        return await service.publish_structured(
            runtime.context,
            title=title,
            sections=sections,
            report_type=report_type,
            analysis_ids=[analysis_id],
            submission_identity=runtime.tool_call_id,
        )

    @tool
    async def validate_draft(
        candidate_path: str,
        report_type: str,
        analysis_ids: list[str],
        runtime: ToolRuntime[RunContext],
        claims_path: str | None = None,
    ) -> dict:
        """Validate template sections, length, evidence and charts without submitting a version."""
        output = await service.validate(
            runtime.context,
            candidate_path=candidate_path,
            report_type=report_type,
            analysis_ids=analysis_ids,
            claims_path=claims_path,
        )
        return {
            key: value for key, value in output.items() if key not in {"body", "claims"}
        }

    @tool
    async def submit_draft(
        candidate_path: str,
        report_type: str,
        analysis_ids: list[str],
        runtime: ToolRuntime[RunContext],
        claims_path: str | None = None,
    ) -> dict:
        """Revalidate the full candidate and submit an immutable draft, including sources and appendix."""
        return await service.submit(
            runtime.context,
            runtime.tool_call_id,
            candidate_path=candidate_path,
            report_type=report_type,
            analysis_ids=analysis_ids,
            claims_path=claims_path,
        )

    @tool
    async def get_analysis(analysis_id: str, runtime: ToolRuntime[RunContext]) -> dict:
        """Read an authorized immutable analysis."""
        return await services.get_object(runtime.context, analysis_id, kind="analysis")

    @tool
    async def get_artifact(artifact_id: str, runtime: ToolRuntime[RunContext]) -> dict:
        """Read an authorized chart receipt and verify its files."""
        return await service.get_artifact(runtime.context, artifact_id)

    return [get_analysis, get_artifact, validate_draft, submit_draft, compose_report]
