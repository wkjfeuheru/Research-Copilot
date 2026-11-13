"""Untrusted result manifests; validation cannot certify arbitrary formula correctness."""

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path, PurePosixPath
import re
import struct
import xml.etree.ElementTree as ET
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from research_copilot.subagents.comparison import ComparisonBasis


def relative_path(value):
    if (
        not value
        or any(c in value for c in ("\\", ":", "\x00"))
        or value.startswith("/")
        or any(p in {"..", "."} for p in value.split("/"))
    ):
        raise ValueError("A relative regular output path is required")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    for part in value.split("/"):
        if (
            part.endswith((" ", "."))
            or part.split(".")[0].upper() in reserved
            or any(ord(c) < 32 for c in part)
        ):
            raise ValueError("Invalid cross-platform output filename")
    return str(PurePosixPath(value))


def finite(value):
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("Decimal string required")
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal") from exc
    if not decimal.is_finite() or abs(decimal.adjusted()) > 100:
        raise ValueError("Finite bounded decimal required")
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InputRef(Contract):
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
    evidence_id: str = Field(pattern=r"^E-[0-9]{6,}$")
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    pointer: str = ""
    raw_value: str
    unit: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    period: str = Field(min_length=1)
    entity: str = Field(min_length=1)
    basis: str = Field(min_length=1)
    scale: str = "1"
    transformation: str = "identity"
    text_start: int | None = Field(default=None, ge=0)
    text_end: int | None = Field(default=None, ge=0)
    extraction: str | None = None


class Metric(Contract):
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
    value: str
    unit: str = Field(min_length=1, max_length=80)
    input_keys: list[str] = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=2000)
    context: dict = Field(default_factory=dict)
    comparison: ComparisonBasis | None = None
    semantic_type: str | None = None
    formula_id: str | None = None
    numerator_basis: str | None = None
    denominator_basis: str | None = None
    is_estimate: bool | None = None
    display_precision: int | None = Field(default=None, ge=0, le=8)
    _finite = field_validator("value")(finite)


class Assumption(Contract):
    value: str | list[str]
    basis: str = Field(min_length=1)
    impact: str = Field(min_length=1)


class ResultManifest(Contract):
    schema_version: int = Field(ge=1, le=2)
    input_refs: list[InputRef] = Field(max_length=500)
    metrics: list[Metric] = Field(max_length=500)
    charts: list[str] = Field(max_length=30)
    assumptions: dict[str, Assumption] = Field(default_factory=dict)
    checks: list[str] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_keys(self):
        if self.schema_version == 2:
            required = (
                "semantic_type",
                "formula_id",
                "numerator_basis",
                "denominator_basis",
                "is_estimate",
                "display_precision",
            )
            for metric in self.metrics:
                if any(
                    getattr(metric, key) is None or getattr(metric, key) == ""
                    for key in required
                ):
                    raise ValueError(
                        "Metric semantic metadata is required for schema_version=2"
                    )
        keys = [i.key for i in self.input_refs] + list(self.assumptions)
        metric_keys = [i.key for i in self.metrics]
        if (
            len(keys) != len(set(keys))
            or len(metric_keys) != len(set(metric_keys))
            or set(keys) & set(metric_keys)
        ):
            raise ValueError("Duplicate input/metric keys")
        available = set(keys)
        for metric in self.metrics:
            if not set(metric.input_keys) <= available:
                raise ValueError("Unknown, forward or cyclic input association")
            if self.schema_version == 2:
                available.add(metric.key)
        for path in self.charts:
            relative_path(path)
        return self


class ChartManifest(Contract):
    schema_version: int = Field(default=1, ge=1, le=1)
    engine: str = Field(pattern=r"^(echarts|matplotlib)$")
    kind: str = Field(
        pattern=r"^(bar|line|scatter|waterfall|sensitivity_heatmap|valuation_range|stacked_bar|candlestick|treemap|sankey)$"
    )
    title: str = Field(min_length=1, max_length=500)
    theme_version: str = Field(pattern=r"^research-v1$")
    width: int = Field(ge=320, le=2400)
    height: int = Field(ge=240, le=1600)
    data_file: str
    option_file: str | None = None
    static_file: str
    svg_file: str | None = None
    metric_keys: list[str] = Field(default_factory=list)
    input_keys: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    alt: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def paths(self):
        for path in (self.data_file, self.option_file, self.static_file, self.svg_file):
            if path is not None:
                relative_path(path)
        if self.engine == "echarts" and not self.option_file:
            raise ValueError("ECharts requires options and a static PNG")
        return self


POLICY_ROOT = (
    Path(__file__).parents[1] / "resources/skills/analysist/financial-modeling/scripts"
)


def validate_option(option):
    policy = json.loads((POLICY_ROOT / "chart_policy.json").read_text("utf-8"))
    if len(json.dumps(option, allow_nan=False).encode()) > policy["maxBytes"]:
        raise ValueError("Chart configuration is too large")
    count = 0

    def visit(value, depth=0, key=""):
        nonlocal count
        count += 1
        if count > policy["maxNodes"] or depth > 20:
            raise ValueError("Chart configuration exceeds limits")
        if isinstance(value, dict):
            for k, v in value.items():
                if k not in policy["keys"]:
                    raise ValueError("Unsupported chart option: " + k)
                if k == "type" and v not in policy["types"]:
                    raise ValueError("Unsupported chart type")
                if k == "renderMode" and v != "richText":
                    raise ValueError("Only text tooltip is allowed")
                if k == "formatter" and v not in policy["formatters"]:
                    raise ValueError("Unapproved formatter")
                visit(v, depth + 1, k)
        elif isinstance(value, list):
            for v in value:
                visit(v, depth + 1, key)
        elif isinstance(value, str):
            if len(value) > 2000 or re.search(
                r"[<>]|(?:https?:|data:|javascript:|file:|//)|\bfunction\s*\(|=>",
                value,
                re.I,
            ):
                raise ValueError("Unsafe chart string")
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ValueError("JSON-only chart data required")

    if (
        not isinstance(option, dict)
        or not isinstance(option.get("series"), list)
        or not option["series"]
    ):
        raise ValueError("Nonempty series required")
    for series in option["series"]:
        if (
            not isinstance(series, dict)
            or series.get("type") not in policy["seriesTypes"]
            or not isinstance(series.get("data"), list)
            or not series["data"]
        ):
            raise ValueError("Supported nonempty chart series required")
    visit(option)
    return option


def validate_png(raw, width, height):
    if (
        len(raw) < 33
        or raw[:8] != b"\x89PNG\r\n\x1a\n"
        or raw[12:16] != b"IHDR"
        or struct.unpack(">II", raw[16:24]) != (width, height)
    ):
        raise ValueError("PNG signature/dimensions mismatch")
    # Pillow is already a Matplotlib dependency; verify chunk CRC/truncation as well.
    from PIL import Image
    import io

    with Image.open(io.BytesIO(raw)) as image:
        image.verify()


def validate_svg(raw):
    if (
        len(raw) > 4 * 1024 * 1024
        or b"<!DOCTYPE" in raw.upper()
        or b"<!ENTITY" in raw.upper()
    ):
        raise ValueError("Unsafe SVG")
    root = ET.fromstring(raw)
    allowed = {
        "svg",
        "g",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "defs",
        "clipPath",
        "linearGradient",
        "radialGradient",
        "stop",
    }
    for element in root.iter():
        if element.tag.split("}")[-1] not in allowed:
            raise ValueError("Unsafe SVG element")
        for key, value in element.attrib.items():
            if (
                key.lower().startswith("on")
                or key.split("}")[-1] in {"href", "src"}
                or re.search(r"(?:https?:|data:|javascript:|file:|//)", value, re.I)
            ):
                raise ValueError("Unsafe SVG resource")
            if "url(" in value.lower() and not re.fullmatch(
                r"url\(#[A-Za-z0-9_-]+\)", value
            ):
                raise ValueError("Unsafe SVG style")
