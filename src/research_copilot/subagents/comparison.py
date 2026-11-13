"""Typed comparison boundaries for attribution; assumptions cannot establish scope."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ComparisonBasis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    current_period: str = Field(min_length=1)
    base_period: str = Field(min_length=1)
    period_basis: Literal["quarter", "half_year", "year", "custom"]
    entity: str = Field(min_length=1)
    reporting_scope: str = Field(min_length=1)
    denominator: str = Field(min_length=1)
    mode: Literal["actual", "scenario"]


def validate_comparison(metric, inputs):
    label = " ".join(
        str(metric.get(k, ""))
        for k in ("key", "local_key", "semantic_type", "formula_id")
    )
    attribution = bool(
        re.search(
            r"attribut|contribution|residual|mix_effect|within_effect|归因|贡献|残差|结构效应",
            label,
            re.I,
        )
    )
    value = metric.get("comparison")
    parents = [
        inputs[k]["comparison"]
        for k in metric["input_keys"]
        if inputs.get(k, {}).get("comparison")
    ]
    if not value and not parents:
        if attribution:
            raise ValueError(
                "Attribution requires explicit comparison periods, scope and denominator; omit unidentifiable effects"
            )
        return None
    comparison = ComparisonBasis.model_validate(value or parents[0]).model_dump()
    for index, key in enumerate(metric["input_keys"]):
        parent = inputs.get(key, {}).get("comparison")
        if not parent or parent == comparison:
            continue
        # Two single-period observations can form a two-period change. This is
        # distinct from subtracting two effects measured over different intervals.
        observation = (
            not attribution
            and metric["formula_id"] in {"difference", "growth_percent"}
            and len(metric["input_keys"]) == 2
            and parent["current_period"] == parent["base_period"]
            and parent["current_period"]
            == comparison["current_period" if index == 0 else "base_period"]
            and all(
                parent[k] == comparison[k]
                for k in (
                    "period_basis",
                    "entity",
                    "reporting_scope",
                    "denominator",
                    "mode",
                )
            )
        )
        if not observation:
            raise ValueError(
                "Incompatible comparison periods/scope/denominator or actual/scenario inputs"
            )
    if attribution and comparison["current_period"] == comparison["base_period"]:
        raise ValueError("comparison requires distinct periods")
    if comparison["mode"] == "actual" and attribution:
        if any(
            re.search(
                r"unknown|unspecified|假设|未知|未披露|未标注|未标明|不明", str(v), re.I
            )
            for v in comparison.values()
        ):
            raise ValueError("Actual attribution comparison has unknown metadata")
        leaves = [
            leaf
            for k in metric["input_keys"]
            for leaf in inputs.get(k, {}).get("inputs", [inputs.get(k, {})])
        ]
        for leaf in leaves:
            if "assumption_key" in leaf or any(
                re.search(
                    r"unknown|unspecified|假设|未知|未披露|未标注|未标明|不明",
                    str(leaf.get(k, "")),
                    re.I,
                )
                for k in ("basis", "currency", "period")
            ):
                raise ValueError(
                    "Actual attribution comparison uses unknown/assumed source metadata; report the gap or a separate scenario"
                )
    return comparison
