"""Numeric disclosures bind to validated facts, never arbitrary source numbers."""

import hashlib
import re


def matched_facts(bindings, facts, evidence, evidence_ids):
    matched, errors = [], []
    if not isinstance(bindings, list) or any(
        not isinstance(item, dict) for item in bindings
    ):
        return [], ["Invalid numeric fact references"]
    for binding in bindings:
        rows = [
            fact["input_ref"]
            for fact in facts
            if all(
                fact["input_ref"].get(key, "1" if key == "scale" else None)
                == binding.get(key, "1" if key == "scale" else None)
                for key in ("key", "evidence_id", "period", "unit", "scale")
            )
        ]
        # Repeated publication of the identical fact is harmless; ambiguity is not.
        unique = {repr(sorted(row.items())): row for row in rows}
        if len(unique) != 1:
            errors.append("numeric fact field/period/unit binding missing or ambiguous")
            continue
        row = next(iter(unique.values()))
        source = evidence.get(row["evidence_id"], {})
        if (
            row["evidence_id"] not in evidence_ids
            or source.get("content_hash") != row["content_hash"]
        ):
            errors.append("numeric fact evidence identity mismatch")
            continue
        matched.append(row)
    return matched, errors


def point_in_time_variants(period):
    """时点类 period 在正文中的常见等价表达；登记值可能是复合写法。"""
    year = re.search(r"((?:19|20)\d{2})", period)
    if not year or not re.search(r"年末|期末|12月31日", period):
        return [period]
    return [f"{year[1]}年末", f"{year[1]}年12月31日", f"{year[1]}年期末"]


def _period_stated(quote, period):
    if period and period in quote:
        return True
    # YTD endpoints are not single quarters. Accept equivalent cumulative labels
    # while retaining the exact registered period in the structured fact binding.
    endpoint = re.search(r"((?:19|20)\d{2})-(03-31|06-30|09-30|12-31)", period)
    if endpoint and (
        re.search(r"累计|YTD|year_to_date", period, re.I)
        or re.search(r"\d{4}-01-01[~/至]" + re.escape(endpoint[0]), period)
    ):
        year, date = endpoint.groups()
        quarter = {"03-31": 1, "06-30": 2, "09-30": 3, "12-31": 4}[date]
        aliases = [
            f"{year}Q{quarter}累计",
            f"{year}Q{quarter} YTD",
            f"{year}年1-{quarter * 3}月",
            f"{year}年{quarter * 3}M累计",
            f"{year} {quarter * 3}M累计",
        ]
        if quarter == 2:
            aliases.extend([f"{year}H1", f"{year}年上半年", f"{year}年半年度"])
        elif quarter == 3:
            aliases.append(f"{year}年前三季度")
        elif quarter == 4 and re.search(
            r"(?<!\d)" + year + r"年(?:度)?(?![度初末底中\d一二三四上下第])", quote
        ):
            return True
        return any(alias in quote for alias in aliases) or bool(
            re.search(re.escape(endpoint[0]) + r"\s*(?:累计|YTD)", quote, re.I)
        )
    # 登记 period 可能是复合写法（如“2024年度（2024-01-01至2024-12-31）”），
    # 正文通常只写前导年度；去掉括号补充说明后再比对（WLY-20261108 第十一轮）。
    base = re.sub(r"[（(][^）)]*[）)]", "", period).strip() or period
    annual = re.fullmatch(r"((?:19|20)\d{2})年度", base)
    if annual:
        return bool(
            re.search(
                r"(?<!\d)" + annual[1] + r"年(?:度)?(?![度初末底中\d一二三四上下第])",
                quote,
            )
        )
    # 时点数（合同负债、期末余额等）正文常写作“2024年末”或“2024年12月31日”，
    # 与登记 period 字面不必相同，按等价表达匹配。
    return any(variant and variant in quote for variant in point_in_time_variants(base))


def fact_mismatch(fact, raw, quote, end):
    """返回事实未能支撑该数字的原因；能支撑时返回 None。

    错误信息要能直接指导修正（缺期间、缺单位、取值不同），否则子代理只能
    反复重读全文猜测原因（WLY-20261108 第十一轮 analysist 30 次调用耗尽）。
    """
    value = fact["raw_value"].replace(",", "").removeprefix("+").removesuffix("%")
    if value != raw:
        return "取值与事实原值不一致"
    if not _period_stated(quote, fact["period"]):
        return f"正文缺少事实期间「{fact['period']}」"
    if not display_unit_matches(quote[end:], fact["unit"], fact.get("scale", "1")):
        return f"数字后缺少事实单位「{fact['unit']}」"
    return None


def fact_supports(fact, raw, quote, end):
    return fact_mismatch(fact, raw, quote, end) is None


def display_unit_matches(text, unit, scale="1"):
    """Match this occurrence's unit, including bounded English display aliases."""
    # 指标单位可能登记为 pct/percent，正文一律用 % 展示，先归一。
    unit = "%" if unit in {"pct", "percent", "％"} else unit
    if unit in {
        "pct_point",
        "percentage_point",
        "percentage points",
        "个百分点",
        "百分点",
    }:
        unit = "pp"
    aliases = {
        "%": ("%", "％", "percent", "pct"),
        "pp": (
            "个百分点",
            "百分点",
            "pp",
            "pct_point",
            "percentage_point",
            "percentage points",
        ),
        "ratio": ("倍", "ratio"),
    }
    accepted = aliases.get(unit, (unit,))
    if unit in {"CNY", "人民币元", "元"}:
        accepted = {
            "1": ("元", "人民币元", "CNY"),
            "1000": ("千元",),
            "10000": ("万元",),
            "100000000": ("亿元",),
        }.get(scale, ())
    elif scale != "1":
        return False
    return any(
        alias
        and re.match(
            re.escape(alias)
            + (r"(?![A-Za-z])" if alias[-1].isascii() and alias[-1].isalpha() else ""),
            text.lstrip(),
        )
        for alias in accepted
    )


def validate_analysis_numbers(candidate, metrics, evidence, facts):
    from research_copilot.subagents.claim_validation import validate_claims

    def check(text, metric_ids, evidence_ids, bindings):
        manifest = {
            "body_hash": hashlib.sha256(text.encode()).hexdigest(),
            "claims": [
                {
                    "start": 0,
                    "end": len(text),
                    "quote": text,
                    "metric_ids": metric_ids,
                    "evidence_ids": evidence_ids,
                    "fact_refs": bindings,
                }
            ],
        }
        return validate_claims(text, manifest, metrics, evidence, [], facts=facts)

    errors = []
    bindings = []
    for claim in candidate.claims:
        refs = [ref.model_dump() for ref in claim.fact_refs]
        bindings.extend(refs)
        errors.extend(check(claim.text, claim.metric_ids, claim.evidence_ids, refs))
    for text in (candidate.summary, *candidate.conclusions):
        errors.extend(
            check(text, candidate.metric_ids, candidate.evidence_ids, bindings)
        )
    return list(dict.fromkeys(errors))
