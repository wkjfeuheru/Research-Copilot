"""Exact text-bound numeric coverage; interpretation remains independently reviewed."""

import hashlib
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

NUMBERS = re.compile(r"[+-]?\d[\d,]*(?:\.\d+)?")
# 估算标注同义词：登记为 is_estimate 的指标在正文每处出现都必须带其一。
_ESTIMATE_MARKERS = re.compile("推算|估算")
EXEMPT = re.compile(
    r"\[\^E-\d+\]|(?<![A-Za-z])E-\d{6}(?!\d)|chart:C-[0-9a-f]+"
    r"|(?<!\d)\d{6}\.(?:SZ|SH|BJ)(?![A-Za-z])"
    r"|\d{4}-\d{2}-\d{2}|\d{4}年\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月"
    r"|\d{4}年(?:0?[1-9]|1[0-2])[-至](?:0?[1-9]|1[0-2])月"
    r"|(?<!\d)(?:19|20)\d{2}(?:年|\s*)(?:3|6|9|12)M(?![A-Za-z0-9.])"
    r"|(?<!\d)\d{1,2}月|(?<!\d)\d{1,2}日|\d{4}年"
    r"|(?<!\d)(?:19|20)\d{2}(?:Q[1-4]|H[12])(?!\d)"
    r"|(?<![A-Za-z0-9])Q[1-4](?!\d)|\bFY(?:19|20)\d{2}\b|^#{1,6}\s+\d+[.、]",
    re.M,
)


def contains_rendered_value(quote, rendered):
    for match in numeric_spans(quote):
        value = Decimal(match.group().replace(",", "").removeprefix("+"))
        if Decimal(rendered) == value or (
            rendered.startswith("-")
            and -Decimal(rendered) == value
            and _decrease_magnitude(quote, match.start(), match.end())
        ):
            return True
    return False


def _decrease_magnitude(quote, start, end):
    before = quote[max(0, start - 16) : start]
    if any(
        word in before
        for word in (
            "减少",
            "下降",
            "降低",
            "下滑",
            "收缩",
            "低于",
            "负增长",
            "降幅",
        )
    ):
        return True
    # The unit and direction must belong to this occurrence, not a different
    # number elsewhere in the claim (e.g. "2.47个百分点的降幅").
    return bool(
        re.match(
            r"\s*(?:个百分点|百分点|pp|pct_point|percentage_point|percentage points)"
            r"\s*(?:的)?(?:降幅|减少幅度|下滑幅度)",
            quote[end:],
        )
    )


def numeric_spans(text, *, identifiers=()):
    exemptions = [m.span() for m in EXEMPT.finditer(text)]
    for identifier in identifiers:
        if not isinstance(identifier, str) or not identifier:
            continue
        exemptions.extend(
            m.span()
            for m in re.finditer(
                r"(?<![A-Za-z0-9_-])" + re.escape(identifier) + r"(?![A-Za-z0-9_-])",
                text,
            )
        )
    return [
        m
        for m in NUMBERS.finditer(text)
        if not any(
            a <= m.start() + int(m.group()[0] in "+-") and m.end() <= b
            for a, b in exemptions
        )
    ]


def render_metric(metric):
    return format(
        Decimal(str(metric["value"])).quantize(
            Decimal(1).scaleb(-metric.get("display_precision", 2)),
            rounding=ROUND_HALF_UP,
        ),
        "f",
    )


def _numeric_hint(raw, quote, end, metric_ids, metrics, bound_facts):
    """给出可直接执行的修正线索：缺期间、缺单位或展示精度不一致。

    只报原始数字时，子代理无法定位原因，只能反复重读全文（WLY-20261108
    第十一轮 analysist 30 次调用耗尽）。
    """
    from research_copilot.subagents.numeric_lineage import fact_mismatch

    for fact in bound_facts:
        reason = fact_mismatch(fact, raw, quote, end)
        if reason and reason != "取值与事实原值不一致":
            return f"（{fact['key']}：{reason}）"
    # 裸四位年份多为“revenue_2024/2023”这类字段简写或漏写“年”，给出可执行改写。
    if re.fullmatch(r"(?:19|20)\d{2}", raw):
        return "（疑似年份或字段标识简写：正文年份须写成「YYYY年」，字段 key 只放在结构化 claims 中）"
    for metric_id in metric_ids:
        metric = metrics.get(metric_id) or {}
        try:
            same_value = Decimal(str(metric["value"])) == Decimal(raw)
        except (KeyError, InvalidOperation):
            continue
        if same_value:
            precision = metric.get("display_precision", 2)
            return (
                f"（{metric_id} 已登记该值，正文需按 display_precision="
                f"{precision} 位展示）"
            )
    return ""


def validate_claims(
    body, manifest, metrics, evidence, conclusions, *, facts=(), required_fields=()
):
    errors, covered, seen = [], [], set()
    rendered_fields = set()
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("claims"), list)
        or len(manifest["claims"]) > 500
    ):
        return ["Invalid claims manifest"]
    for claim in manifest["claims"]:
        if not isinstance(claim, dict) or any(
            not isinstance(claim.get(key, []), list)
            or any(not isinstance(i, str) for i in claim.get(key, []))
            for key in ("metric_ids", "evidence_ids")
        ):
            return ["Invalid claim reference list"]
    if manifest.get("body_hash") != hashlib.sha256(body.encode()).hexdigest():
        return ["claims body_hash mismatch"]
    by_id = {c["claim_id"]: c for c in conclusions if isinstance(c, dict)}
    for claim in manifest.get("claims", []):
        start, end = claim.get("start"), claim.get("end")
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(body)
            or body[start:end] != claim.get("quote")
        ):
            errors.append("claim exact span mismatch")
            continue
        quote = body[start:end]
        mids, eids = claim.get("metric_ids", []), claim.get("evidence_ids", [])
        if (
            not (mids or eids)
            or not set(mids) <= metrics.keys()
            or not set(eids) <= evidence.keys()
        ):
            errors.append("claim references unauthorized or missing inputs")
            continue
        cid = claim.get("conclusion_id")
        conclusion = by_id.get(cid) if cid else None
        from research_copilot.subagents.numeric_lineage import (
            matched_facts,
            fact_supports,
            display_unit_matches,
        )

        bound_facts, fact_errors = matched_facts(
            claim.get("fact_refs", []), facts, evidence, eids
        )
        errors.extend(fact_errors)
        if cid:
            conclusion_mids = (
                set(conclusion.get("metric_ids", [])) if conclusion else set()
            )
            conclusion_eids = (
                set(conclusion.get("evidence_ids", [])) if conclusion else set()
            )
            if not conclusion or not (
                set(mids) & conclusion_mids or set(eids) & conclusion_eids
            ):
                errors.append("claim conclusion lineage mismatch")
            else:
                seen.add(cid)
        # Only explicit, authorized metric bindings may exempt their local keys.
        # Unknown identifiers and independent numbers still require validation.
        for match in numeric_spans(
            quote, identifiers=[metrics[mid].get("local_key") for mid in mids]
        ):
            raw = match.group().replace(",", "").removeprefix("+")
            supporting_facts = [
                fact
                for fact in bound_facts
                if fact_supports(fact, raw, quote, match.end())
            ]
            rendered_fields.update(fact["key"] for fact in supporting_facts)
            supported = False
            for mid in mids:
                metric = metrics[mid]
                precision = metric.get("display_precision", 2)
                try:
                    rendered = format(
                        Decimal(str(metric["value"])).quantize(
                            Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP
                        ),
                        "f",
                    )
                except (InvalidOperation, KeyError):
                    continue
                magnitude = (
                    rendered.startswith("-")
                    and Decimal(raw) == -Decimal(rendered)
                    and _decrease_magnitude(quote, match.start(), match.end())
                )
                if Decimal(raw) != Decimal(rendered) and not magnitude:
                    continue
                supported = True
                # A disclosure elsewhere in a large claim cannot qualify this occurrence.
                after = quote[match.end() : match.end() + 18]
                if metric.get("is_estimate") and not _ESTIMATE_MARKERS.search(quote):
                    errors.append(
                        "estimate must be marked at every occurrence"
                        "（估算数字须就近标注「推算」或「估算」）: " + raw
                    )
                unit = metric.get("unit")
                unit_matches = display_unit_matches(after, unit)
                if unit and not unit_matches:
                    errors.append(
                        f"metric display unit mismatch: {mid} {raw} requires {unit}"
                    )
            registered = [
                mid
                for mid, metric in metrics.items()
                if Decimal(render_metric(metric)) == Decimal(raw)
            ]
            if registered and not set(registered) & set(mids):
                errors.append("registered metric must be explicitly referenced: " + raw)
            if not supported and not registered:
                supported = bool(supporting_facts)
            if not supported:
                errors.append(
                    "numeric claim lacks matching value/precision: "
                    + raw
                    + _numeric_hint(raw, quote, match.end(), mids, metrics, bound_facts)
                    + " | context: "
                    + quote[max(0, match.start() - 60) : match.end() + 60]
                )
        covered.append((start, end))
    for match in numeric_spans(body):
        if any(a <= match.start() and match.end() <= b for a, b in covered):
            continue
        start, end = max(0, match.start() - 60), min(len(body), match.end() + 60)
        errors.append(
            "uncovered numeric claim: "
            + match.group()
            + " | context: "
            + body[start:end].replace("\n", " ")
        )
    for cid, conclusion in by_id.items():
        if not conclusion.get("required_in_report"):
            continue
        if cid not in seen:
            errors.append("missing required conclusion: " + cid)
            continue
        for mid in conclusion.get("metric_ids", []):
            metric = metrics.get(mid)
            if metric is None:
                continue
            rendered = render_metric(metric)
            if not contains_rendered_value(body, rendered):
                errors.append(f"required conclusion value missing: {cid}: {rendered}")
    errors.extend(
        "required fact not rendered: " + key
        for key in required_fields
        if key not in rendered_fields
    )
    return list(dict.fromkeys(errors))
