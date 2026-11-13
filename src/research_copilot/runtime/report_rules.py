"""Load and validate config/compliance_rules.yaml before a model call."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

SUPPORTED_RULES_VERSION = 1
MAX_RULES_BYTES = 64 * 1024
MAX_TEMPLATE_CHARS = 4000
EXAMPLE_MARKER = "示例规则"

REQUIRED_TEMPLATE_FIELDS: tuple[str, ...] = (
    "risk_warning_template",
    "disclaimer_template",
)


class ReportRulesError(ValueError):
    """规则文件缺失、越界或格式错误。"""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReportRules:
    version: int
    sensitive_words: tuple[str, ...]
    required_disclosures: tuple[str, ...]
    prohibited_patterns: tuple[str, ...]
    risk_warning_template: str
    disclaimer_template: str
    content: Mapping[str, Any]
    content_hash: str
    path: Path | None = None
    is_example: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            **dict(self.content),
            "content_hash": self.content_hash,
            "is_example": self.is_example,
        }


def load_report_rules(
    path: Path | str,
    *,
    max_bytes: int = MAX_RULES_BYTES,
) -> ReportRules:
    target = Path(path)
    if not target.is_file():
        raise ReportRulesError(
            f"报告规则文件不存在：{target}；写作前必须由研究员提供合规规则"
        )
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise ReportRulesError(f"无法读取报告规则文件：{target}") from exc
    if size > max_bytes:
        raise ReportRulesError(
            f"报告规则文件超过 {max_bytes // 1024} KiB 上限：{target}"
        )
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReportRulesError(
            f"报告规则文件必须以 UTF-8 读取：{target}（{type(exc).__name__}）"
        ) from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ReportRulesError(
            f"报告规则 YAML 解析失败：{target}（{_yaml_problem(exc)}）"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ReportRulesError(f"报告规则顶层必须是 YAML 映射：{target}")

    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ReportRulesError(f"报告规则 version 必须是整数：{target}")
    if version != SUPPORTED_RULES_VERSION:
        raise ReportRulesError(
            f"报告规则 version 不受支持：{version}；当前只支持 {SUPPORTED_RULES_VERSION}"
        )

    sensitive_words = _string_list(raw, "sensitive_words", target)
    required_disclosures = _string_list(raw, "required_disclosures", target)
    prohibited_patterns = _string_list(raw, "prohibited_patterns", target)
    for pattern in prohibited_patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ReportRulesError(
                f"报告规则 prohibited_patterns 含非法正则：{pattern!r}（{exc.msg}）"
            ) from exc

    risk_warning = _template(raw, "risk_warning_template", target)
    disclaimer = _template(raw, "disclaimer_template", target)
    content = {
        "version": version,
        "sensitive_words": list(sensitive_words),
        "required_disclosures": list(required_disclosures),
        "prohibited_patterns": list(prohibited_patterns),
        "risk_warning_template": risk_warning,
        "disclaimer_template": disclaimer,
    }
    if "test_only" in raw:
        if type(raw["test_only"]) is not bool:
            raise ReportRulesError("test_only must be a boolean")
        content["test_only"] = raw["test_only"]
    return ReportRules(
        version=version,
        sensitive_words=sensitive_words,
        required_disclosures=required_disclosures,
        prohibited_patterns=prohibited_patterns,
        risk_warning_template=risk_warning,
        disclaimer_template=disclaimer,
        content=content,
        content_hash=_sha256_text(_canonical_json(content)),
        path=target,
        is_example=EXAMPLE_MARKER in risk_warning or EXAMPLE_MARKER in disclaimer,
    )


def _string_list(raw: Mapping[str, Any], field: str, path: Path) -> tuple[str, ...]:
    value = raw.get(field, [])
    if not isinstance(value, list):
        raise ReportRulesError(f"报告规则 {field} 必须是字符串列表：{path}")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ReportRulesError(f"报告规则 {field} 必须是字符串列表：{path}")
        text = item.strip()
        if not text:
            raise ReportRulesError(f"报告规则 {field} 不能包含空字符串：{path}")
        result.append(text)
    return tuple(result)


def _template(raw: Mapping[str, Any], field: str, path: Path) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReportRulesError(f"报告规则缺少非空 {field}：{path}")
    text = value.strip()
    if len(text) > MAX_TEMPLATE_CHARS:
        raise ReportRulesError(
            f"报告规则 {field} 超过 {MAX_TEMPLATE_CHARS} 字符上限：{path}"
        )
    return text


def _yaml_problem(exc: yaml.YAMLError) -> str:
    problem = getattr(exc, "problem", None)
    mark = getattr(exc, "problem_mark", None)
    if problem and mark is not None:
        return f"第 {mark.line + 1} 行：{problem}"
    return type(exc).__name__
