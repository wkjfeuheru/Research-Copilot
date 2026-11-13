"""Bounded compliance scan. The regex pass runs in a child process."""

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

APPENDIX_IDS: tuple[str, ...] = ("risk_warning", "disclaimer")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_HITS = 500
MAX_HITS_PER_RULE = 100
DEFAULT_SCAN_TIMEOUT_SECONDS = 10.0
MAX_SCAN_TEXT_CHARS = 300_000
HitKind = Literal["sensitive_word", "prohibited_pattern", "missing_disclosure"]


class ComplianceScanError(RuntimeError):
    """规则扫描执行失败。"""


class ComplianceScanTimeout(ComplianceScanError):
    """扫描进程超时。这是执行失败，不是无命中。"""


class ComplianceRuleError(ValueError):
    """规则快照不合法。"""


@dataclass(frozen=True, slots=True)
class RawRuleHit:
    kind: HitKind
    rule_id: str
    fragment: str
    start: int | None = None
    end: int | None = None
    appendix_id: str | None = None
    context: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "rule_id": self.rule_id,
            "fragment": self.fragment,
            "start": self.start,
            "end": self.end,
            "appendix_id": self.appendix_id,
            "context": self.context,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RawRuleHit":
        kind = str(payload.get("kind") or "")
        if kind not in ("sensitive_word", "prohibited_pattern", "missing_disclosure"):
            raise ComplianceScanError(f"扫描结果包含未知命中类型：{kind}")
        start = payload.get("start")
        end = payload.get("end")
        return cls(
            kind=kind,  # type: ignore[arg-type]
            rule_id=str(payload.get("rule_id") or ""),
            fragment=str(payload.get("fragment") or ""),
            start=int(start) if isinstance(start, int) else None,
            end=int(end) if isinstance(end, int) else None,
            appendix_id=(
                str(payload.get("appendix_id"))
                if payload.get("appendix_id") is not None
                else None
            ),
            context=str(payload.get("context") or ""),
        )


@dataclass(frozen=True, slots=True)
class ScanRules:
    version: int
    content_hash: str
    sensitive_words: tuple[str, ...]
    prohibited_patterns: tuple[str, ...]
    required_disclosures: tuple[str, ...]
    templates: Mapping[str, str]
    is_example: bool = False
    compiled: tuple[tuple[str, re.Pattern[str]], ...] = field(default=())


def normalize_scan_rules(rules: Mapping[str, Any]) -> ScanRules:
    if not isinstance(rules, Mapping):
        raise ComplianceRuleError("合规规则快照必须是对象")
    try:
        version = int(rules.get("version") or 0)
    except (TypeError, ValueError) as exc:
        raise ComplianceRuleError("合规规则 version 不合法") from exc
    content_hash = str(rules.get("content_hash") or "").strip()
    if not SHA256_RE.match(content_hash):
        raise ComplianceRuleError("合规规则快照缺少合法 content_hash")

    sensitive = _string_list(rules.get("sensitive_words"), "sensitive_words")
    patterns = _string_list(rules.get("prohibited_patterns"), "prohibited_patterns")
    required = _string_list(rules.get("required_disclosures"), "required_disclosures")
    unknown = [item for item in required if item not in APPENDIX_IDS]
    if unknown:
        raise ComplianceRuleError(
            "required_disclosures 只支持 risk_warning / disclaimer；未知键："
            + "、".join(unknown)
        )

    compiled: list[tuple[str, re.Pattern[str]]] = []
    for index, pattern in enumerate(patterns, start=1):
        try:
            compiled.append((f"prohibited-{index:03d}", re.compile(pattern)))
        except re.error as exc:
            raise ComplianceRuleError(
                f"禁止表述规则 {index} 不是合法正则：{exc.msg}"
            ) from exc

    templates: dict[str, str] = {}
    for key in APPENDIX_IDS:
        value = rules.get(f"{key}_template")
        if not isinstance(value, str) or not value.strip():
            raise ComplianceRuleError(f"合规规则快照缺少 {key}_template")
        templates[key] = value.strip()

    return ScanRules(
        version=version,
        content_hash=content_hash,
        sensitive_words=sensitive,
        prohibited_patterns=patterns,
        required_disclosures=required,
        templates=templates,
        is_example=bool(rules.get("is_example")),
        compiled=tuple(compiled),
    )


def scan_rules_inprocess(payload: Mapping[str, Any]) -> tuple[RawRuleHit, ...]:
    text = payload.get("text")
    if not isinstance(text, str):
        raise ComplianceScanError("扫描输入缺少 text")
    if len(text) > MAX_SCAN_TEXT_CHARS:
        raise ComplianceScanError(
            f"扫描文本超过 {MAX_SCAN_TEXT_CHARS} 字符上限，不能静默截断"
        )
    raw_rules = payload.get("rules")
    rules = normalize_scan_rules(raw_rules if isinstance(raw_rules, Mapping) else {})
    appendix_texts = payload.get("appendix_texts")
    appendix = appendix_texts if isinstance(appendix_texts, Mapping) else {}

    hits: list[RawRuleHit] = []
    for index, word in enumerate(rules.sensitive_words, start=1):
        _scan_literal(
            text,
            word,
            rule_id=f"sensitive-{index:03d}",
            kind="sensitive_word",
            hits=hits,
        )
        if len(hits) >= MAX_HITS:
            break
    if len(hits) < MAX_HITS:
        for rule_id, pattern in rules.compiled:
            _scan_pattern(text, pattern, rule_id=rule_id, hits=hits)
            if len(hits) >= MAX_HITS:
                break

    for key in rules.required_disclosures:
        appendix_text = appendix.get(key)
        if not isinstance(appendix_text, str) or not appendix_text.strip():
            hits.append(
                RawRuleHit(
                    kind="missing_disclosure",
                    rule_id=f"disclosure-{key}",
                    fragment=f"缺少强制披露附录：{key}",
                    appendix_id=key,
                    context="",
                )
            )
            continue
        template = rules.templates[key]
        if template not in appendix_text:
            hits.append(
                RawRuleHit(
                    kind="missing_disclosure",
                    rule_id=f"disclosure-{key}-template",
                    fragment=f"{key} 附录未包含冻结规则模板文本",
                    appendix_id=key,
                    context=_sentence_at(appendix_text, 0),
                )
            )

    text_hits = [item for item in hits if item.start is not None]
    other_hits = [item for item in hits if item.start is None]
    text_hits.sort(key=lambda item: (item.start or 0, item.end or 0, item.rule_id))
    return tuple(text_hits + other_hits)


def scan_rules_subprocess(
    payload: Mapping[str, Any],
    *,
    timeout: float = DEFAULT_SCAN_TIMEOUT_SECONDS,
    python_executable: str | None = None,
) -> tuple[RawRuleHit, ...]:
    if timeout <= 0:
        raise ComplianceScanTimeout("扫描超时设置不合法")
    command = [
        python_executable or sys.executable,
        "-m",
        "research_copilot.review.rule_scan_worker",
    ]
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ComplianceScanTimeout(
            f"规则扫描在 {timeout:g} 秒内未完成；属于执行失败，不是无命中"
        ) from exc
    except OSError as exc:
        raise ComplianceScanError(
            f"无法启动规则扫描进程（{type(exc).__name__}）"
        ) from exc

    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ComplianceScanError(
            "规则扫描进程失败："
            + (detail[:200] if detail else f"退出码 {completed.returncode}")
        )
    try:
        response = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ComplianceScanError("规则扫描进程返回了无法解析的结果") from exc
    raw_hits = response.get("hits") if isinstance(response, Mapping) else None
    if not isinstance(raw_hits, list):
        raise ComplianceScanError("规则扫描进程返回结果缺少 hits")
    return tuple(
        RawRuleHit.from_payload(item) for item in raw_hits if isinstance(item, Mapping)
    )


def _scan_literal(
    text: str,
    word: str,
    *,
    rule_id: str,
    kind: HitKind,
    hits: list[RawRuleHit],
) -> None:
    if not word:
        return
    start = 0
    count = 0
    while count < MAX_HITS_PER_RULE:
        position = text.find(word, start)
        if position < 0:
            break
        end = position + len(word)
        hits.append(
            RawRuleHit(
                kind=kind,
                rule_id=rule_id,
                fragment=word,
                start=position,
                end=end,
                context=_sentence_at(text, position),
            )
        )
        start = end
        count += 1


def _scan_pattern(
    text: str,
    pattern: re.Pattern[str],
    *,
    rule_id: str,
    hits: list[RawRuleHit],
) -> None:
    count = 0
    for match in pattern.finditer(text):
        fragment = match.group(0)
        if not fragment:
            continue
        hits.append(
            RawRuleHit(
                kind="prohibited_pattern",
                rule_id=rule_id,
                fragment=fragment,
                start=match.start(),
                end=match.end(),
                context=_sentence_at(text, match.start()),
            )
        )
        count += 1
        if count >= MAX_HITS_PER_RULE or len(hits) >= MAX_HITS:
            break


def _sentence_at(text: str, position: int) -> str:
    start = max(
        text.rfind("。", 0, position),
        text.rfind("；", 0, position),
        text.rfind("\n", 0, position),
        text.rfind("！", 0, position),
        text.rfind("？", 0, position),
    )
    end_candidates = [
        index
        for index in (
            text.find("。", position),
            text.find("；", position),
            text.find("\n", position),
            text.find("！", position),
            text.find("？", position),
        )
        if index >= 0
    ]
    end = min(end_candidates) + 1 if end_candidates else len(text)
    return text[start + 1 : end].strip()


def _string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ComplianceRuleError(f"{field_name} 必须是字符串列表")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ComplianceRuleError(f"{field_name} 不能包含空字符串")
        result.append(item.strip())
    return tuple(result)
