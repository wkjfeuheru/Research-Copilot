"""Render explicit, authorized references; never infer lineage from equal numbers."""

import re

from research_copilot.subagents.contracts import (
    BusinessInputError,
    ReportSection,
    SectionClaim,
)
from research_copilot.subagents.claim_validation import render_metric

TOKEN = re.compile(r"\{\{(fact|metric|conclusion):([^{}]+)\}\}")


def render_sections(title, texts, headings, payload, metrics):
    if len(texts) != len(headings):
        raise BusinessInputError(
            f"Expected {len(headings)} section texts in template order: {headings}"
        )
    facts = {}
    for row in payload.get("verified_facts", []):
        ref = row["input_ref"]
        if ref["evidence_id"] in payload["evidence_ids"]:
            facts.setdefault(ref["key"], []).append(ref)
    conclusions = {c["claim_id"]: c for c in payload.get("claims", [])}
    seen = set()

    def expand(text):
        claims = []

        def replace(match):
            kind, key = match.groups()
            mids, eids, refs, cid = [], [], [], None
            if kind == "fact":
                rows = facts.get(key, [])
                if len(rows) != 1:
                    raise BusinessInputError(f"Unknown or ambiguous fact key: {key}")
                fact = rows[0]
                if fact.get("scale", "1") != "1":
                    raise BusinessInputError(
                        f"Fact {key} requires explicit scaled-unit rendering"
                    )
                unit = fact["unit"]
                raw = fact["raw_value"]
                period = fact["period"]
                endpoint = re.search(
                    r"((?:19|20)\d{2})-(03-31|06-30|09-30|12-31)", period
                )
                if endpoint and (
                    re.search(r"累计|YTD|year_to_date", period, re.I)
                    or re.search(r"\d{4}-01-01[~/至]" + re.escape(endpoint[0]), period)
                ):
                    q = {"03-31": 1, "06-30": 2, "09-30": 3, "12-31": 4}[endpoint[2]]
                    period = f"{endpoint[1]}Q{q}累计"
                value = (
                    period
                    + "："
                    + raw
                    + ("" if raw.endswith("%") and unit == "%" else unit)
                )
                refs = [
                    {
                        k: fact.get(k, "1" if k == "scale" else None)
                        for k in ("key", "evidence_id", "period", "unit", "scale")
                    }
                ]
                eids = [fact["evidence_id"]]
            elif kind == "metric":
                if key not in payload["metric_ids"] or key not in metrics:
                    raise BusinessInputError(f"Unknown metric ID: {key}")
                metric = metrics[key]
                unit = metric.get("unit", "")
                unit = (
                    "个百分点"
                    if unit in {"pp", "pct_point", "percentage_point"}
                    else unit
                )
                value = render_metric(metric) + unit + "（推算）"
                mids, eids = (
                    [key],
                    list(metric.get("evidence_ids", payload["evidence_ids"])),
                )
            else:
                if key not in conclusions:
                    raise BusinessInputError(f"Unknown conclusion ID: {key}")
                item = conclusions[key]
                value, mids, eids = (
                    item["text"],
                    item.get("metric_ids", []),
                    item.get("evidence_ids", []),
                )
                refs, cid = item.get("fact_refs", []), key
                seen.add(key)
            claims.append(
                SectionClaim(
                    quote=value,
                    metric_ids=mids,
                    evidence_ids=eids,
                    fact_refs=refs,
                    conclusion_id=cid,
                )
            )
            return value + "".join(f"[^{eid}]" for eid in eids)

        rendered = TOKEN.sub(replace, text)
        if "{{" in rendered or "}}" in rendered:
            raise BusinessInputError("Invalid reference placeholder")
        return rendered, claims

    rendered_title, title_claims = expand(title or "")
    sections = []
    for heading, text in zip(headings, texts):
        rendered, claims = expand(text)
        sections.append(ReportSection(heading=heading, text=rendered, claims=claims))
    # A title is independently audited, with exactly the references it declares.
    if title_claims:
        sections[0] = sections[0].model_copy(
            update={
                "claims": [
                    *sections[0].claims,
                    SectionClaim(
                        quote=rendered_title,
                        metric_ids=list(
                            dict.fromkeys(m for c in title_claims for m in c.metric_ids)
                        ),
                        evidence_ids=list(
                            dict.fromkeys(
                                e for c in title_claims for e in c.evidence_ids
                            )
                        ),
                        fact_refs=[f for c in title_claims for f in c.fact_refs],
                    ),
                ]
            }
        )
    missing = [
        key
        for key, c in conclusions.items()
        if c.get("required_in_report") and key not in seen
    ]
    if missing:
        raise BusinessInputError(
            "Missing required conclusion placeholders: " + ", ".join(missing)
        )
    return rendered_title, sections
