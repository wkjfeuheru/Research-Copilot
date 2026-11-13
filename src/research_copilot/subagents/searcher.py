"""Schema-preserving adapters for approved read-only sources."""

from dataclasses import dataclass
from contextlib import asynccontextmanager
from functools import partial
import hashlib
import json
import re
from typing import Callable

from jsonschema import Draft202012Validator
from langchain.tools import ToolRuntime
from research_copilot.contracts import RunContext
from langchain_core.tools import StructuredTool
from research_copilot.subagents.source_policy import parse_research_as_of, qualify_text

SECRET_FIELDS = {
    "token",
    "access_token",
    "api_key",
    "authorization",
    "x-auth-token",
    "password",
    "secret",
}
_MCP_PREFIXES = {
    "financekb_market": "financekb_",
    "financekb_fundamentals": "financekb_",
    "financekb_macro": "financekb_",
    "financekb_news": "financekb_",
    "financekb_knowledge": "financekb_",
    "financekb_futures": "financekb_",
}
_FINANCEKB_PREFIX = "financekb_"
#: Bumped whenever normalization changes what an identical response yields.
NORMALIZATION_VERSION = 2
_FINANCEKB_PUBLICATION_FIELDS = {
    "get_financial_statements": "announcement_date",
    "get_dividend_history": "announcement_date",
    "get_macro_indicator": "publication_date",
    "search_news": "published_at",
    "get_filings": "disclosure_date",
    "get_earnings_calendar": "actual_disclosure_date",
}


def redact(value, secrets):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in SECRET_FIELDS else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


@asynccontextmanager
async def open_mcp_sources(parameters, *, server, approved, call_timeout=None):
    """Host lifecycle: no implicit connection retry or authentication fallback."""
    from research_copilot.sources.mcp_client import (
        DEFAULT_CALL_TIMEOUT_SECONDS,
        MCPClient,
        MCPToolResult,
    )
    from urllib.parse import urlsplit, parse_qs

    address = urlsplit(parameters.url or "")
    if (
        parameters.transport != "http"
        or address.scheme != "https"
        or address.username
        or address.password
        or set(parse_qs(address.query)) & SECRET_FIELDS
    ):
        raise ValueError("Source requires HTTPS and header-only authentication")
    headers = parameters.headers or {}
    secrets = tuple(headers.values())
    authorization = headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        secrets += (authorization.removeprefix("Bearer "),)

    def filter_result(result):
        return MCPToolResult(
            result.is_error,
            redact(result.structured, secrets),
            tuple(redact(list(result.text_blocks), secrets)),
            result.unsupported_blocks,
        )

    async with MCPClient(
        parameters,
        name=server,
        max_crash_restarts=0,
        call_timeout=(
            DEFAULT_CALL_TIMEOUT_SECONDS if call_timeout is None else call_timeout
        ),
        result_filter=filter_result,
    ) as client:
        yield await discover_mcp_sources(
            client,
            server=server,
            approved=approved,
            redact_values=secrets,
        )


@dataclass(frozen=True)
class SourceTool:
    name: str
    server: str
    original_name: str
    description: str
    schema: dict
    fetch: Callable
    kind: str = "financial_data"
    schema_hash: str | None = None
    normalize: Callable | None = None
    observed_schema_hash: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceDraft:
    """One evidence unit derived from a single fetched response."""

    raw: object
    kind: str
    #: Stable identity across retrieval noise (scores, ordering, latency).
    identity: str | None = None
    pointer: str | None = None


@dataclass(frozen=True, slots=True)
class Acquisition:
    """Normalised outcome of one source call; the full response is journaled separately."""

    drafts: tuple[EvidenceDraft, ...] = ()
    warnings: tuple[str, ...] = ()
    pagination: dict | None = None


_KNOWLEDGE_COLLECTIONS = {
    "search_research": "finance_report",
    "search_policy": "finance_policy",
}
_KNOWLEDGE_TITLE_FIELDS = ("title", "name")
_KNOWLEDGE_BODY_FIELDS = ("body", "content", "text", "snippet", "abstract")
_KNOWLEDGE_DATE_FIELDS = (
    "date",
    "published_at",
    "publish_date",
    "publication_date",
    "published",
)
_KNOWLEDGE_ID_FIELDS = (
    "id",
    "doc_id",
    "document_id",
    "source_id",
    "chunk_id",
    "chunk",
)
_KNOWLEDGE_PAGE_LIMIT = 200
_KNOWLEDGE_PAGE_SIZE_LIMIT = 50


def _first_text(item, fields):
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _stable_identity(fields):
    return hashlib.sha256(
        json.dumps(
            fields,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _structured_envelope(raw, *, domain, validator):
    """Validate the query envelope before anything reaches the journal."""

    if (
        not isinstance(raw, dict)
        or not validator.is_valid(raw)
        or raw.get("domain") != domain
        or not isinstance(raw.get("available"), bool)
        or not isinstance(raw.get("items"), list)
    ):
        raise SourceUnavailable(
            "FinanceKB response does not match its approved output schema",
            code="contract_mismatch",
        )


def _knowledge_envelope(raw, *, tool_name, validator):
    if not isinstance(raw, dict) or not validator.is_valid(raw):
        raise SourceUnavailable(
            "FinanceKB response does not match its approved output schema",
            code="contract_mismatch",
        )
    if raw.get("collection") != _KNOWLEDGE_COLLECTIONS[tool_name]:
        raise SourceUnavailable(
            "FinanceKB knowledge collection does not match the requested tool",
            code="contract_mismatch",
        )
    page, page_size = raw.get("page"), raw.get("page_size")
    returned, items = raw.get("returned"), raw.get("items")
    has_more, next_page = raw.get("has_more"), raw.get("next_page")
    if (
        not isinstance(page, int)
        or isinstance(page, bool)
        or page < 1
        or not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or not 1 <= page_size <= _KNOWLEDGE_PAGE_SIZE_LIMIT
        or page * page_size > _KNOWLEDGE_PAGE_LIMIT
        or not isinstance(returned, int)
        or isinstance(returned, bool)
        or not isinstance(items, list)
        or returned != len(items)
        or len(items) > page_size
        or not isinstance(has_more, bool)
    ):
        raise SourceUnavailable(
            "FinanceKB knowledge pagination is inconsistent",
            code="contract_mismatch",
        )
    if has_more and (
        not isinstance(next_page, int)
        or isinstance(next_page, bool)
        or next_page <= page
    ):
        raise SourceUnavailable(
            "FinanceKB knowledge pagination does not advance",
            code="contract_mismatch",
        )
    if not has_more and next_page is not None:
        raise SourceUnavailable(
            "FinanceKB knowledge pagination is inconsistent",
            code="contract_mismatch",
        )


def _knowledge_pagination(page):
    return {
        "collection": page.get("collection"),
        "as_of": page.get("as_of"),
        "page": page.get("page"),
        "page_size": page.get("page_size"),
        "returned": page.get("returned"),
        "has_more": page.get("has_more"),
        "next_page": page.get("next_page"),
        "window_limit_reached": page["page"] * page["page_size"]
        >= _KNOWLEDGE_PAGE_LIMIT,
    }


def _structured_normalizer(raw, brief, *, domain):
    structured = raw["structured"]
    if (
        not isinstance(structured, dict)
        or structured.get("domain") != domain
        or not isinstance(structured.get("items"), list)
    ):
        return Acquisition(warnings=("FinanceKB 响应结构无效，未登记证据。",))
    if structured.get("available") is not True:
        return Acquisition(warnings=("本次查询数据不可用，未登记证据。",))
    if not structured["items"]:
        return Acquisition(warnings=("查询成功但无匹配记录。",))
    return Acquisition(drafts=(EvidenceDraft(raw=raw, kind="financial_data"),))


def _knowledge_normalizer(raw, brief, *, tool_name):
    page = raw["structured"]
    if not isinstance(page, dict) or not isinstance(page.get("items"), list):
        return Acquisition(warnings=("FinanceKB 响应结构无效，未登记知识片段。",))
    pagination = _knowledge_pagination(page)
    warnings: list[str] = []
    drafts: list[EvidenceDraft] = []
    as_of = str(brief.get("as_of") or "")
    try:
        boundary = parse_research_as_of(as_of)
    except ValueError:
        return Acquisition(
            warnings=("任务信息截止时点无效，未登记知识片段。",),
            pagination=pagination,
        )
    if not page["items"]:
        return Acquisition(
            warnings=("查询成功但无匹配知识片段。",),
            pagination=pagination,
        )
    for index, item in enumerate(page["items"]):
        label = f"第{index + 1}条命中"
        if not isinstance(item, dict):
            warnings.append(f"{label}结构异常，已保留审计未登记。")
            continue
        title = _first_text(item, _KNOWLEDGE_TITLE_FIELDS)
        body = _first_text(item, _KNOWLEDGE_BODY_FIELDS)
        if not body:
            warnings.append(f"{label}缺少片段正文，已保留审计未登记。")
            continue
        qualification = qualify_text(title + "\n" + body, brief)
        if qualification["relevant"] is not True:
            warnings.append(f"{label}与研究主题不匹配，已保留审计未登记。")
            continue
        date_raw = _first_text(item, _KNOWLEDGE_DATE_FIELDS)
        if not date_raw:
            warnings.append(f"{label}缺少发布日期，已保留审计未登记。")
            continue
        try:
            published = parse_research_as_of(date_raw)
        except ValueError:
            warnings.append(f"{label}发布日期无法解析，已保留审计未登记。")
            continue
        if published > boundary:
            warnings.append(f"{label}晚于研究截止时点，已保留审计未登记。")
            continue
        stable = {
            "source": item.get("source"),
            "id": _first_text(item, _KNOWLEDGE_ID_FIELDS),
            "chunk": item.get("chunk"),
            "page": item.get("page"),
            "heading_path": item.get("heading_path"),
            "broker": item.get("broker"),
            "report_type": item.get("report_type"),
            "title": title,
            "body": body,
            "date": date_raw,
        }
        drafts.append(
            EvidenceDraft(
                raw={
                    "structured": item,
                    "title": title[:300],
                    "body": body,
                    "published_at": date_raw,
                    "qualification": qualification,
                    "source_ref": {
                        "collection": page.get("collection"),
                        "page": page.get("page"),
                        "item_index": index,
                    },
                },
                kind="search_snippet",
                identity=_stable_identity(stable),
                pointer=f"/items/{index}",
            )
        )
    if pagination["window_limit_reached"]:
        warnings.append("已达到分页窗口上限，覆盖范围有限。")
    return Acquisition(tuple(drafts), tuple(warnings[:8]), pagination)


async def discover_mcp_sources(client, *, server, approved, redact_values=()):
    if server not in _MCP_PREFIXES:
        raise ValueError("Unapproved MCP source")
    sources = []
    prefix = _MCP_PREFIXES[server]
    financekb = prefix == "financekb_"
    knowledge = server == "financekb_knowledge"
    domain = server.removeprefix(prefix)
    from research_copilot.sources.financekb_contracts import CONTRACTS
    from research_copilot.sources.schema_compatibility import compare_schemas

    contract = CONTRACTS.get(domain)
    pinned = {tool.name: tool for tool in contract.tools} if contract else {}
    for info in (
        await client.list_tools()
    ):  # Existing owner handles initialize and paginated tools/list.
        if info.name not in approved:
            continue
        reviewed = pinned.get(info.name)
        if reviewed is not None and reviewed.schema_fingerprint != approved[info.name]:
            reviewed = None
        if approved[info.name] != info.schema_fingerprint:
            comparison = compare_schemas(
                reviewed.input_schema, reviewed.output_schema,
                info.input_schema, info.output_schema,
            ) if reviewed and reviewed.complete else None
            if not comparison or not comparison["compatible"]:
                raise ValueError("Approved MCP schema changed")
        if financekb and (info.read_only_hint is not True or not info.output_schema):
            raise ValueError("FinanceKB tools must be read-only with an output schema")
        if info.read_only_hint is False or re.search(
            r"\b(delete|write|update|execute|trade|order|insert)\b",
            info.name.replace("_", " "),
            re.I,
        ):
            raise ValueError("Only reviewed read-only MCP tools are allowed")
        if knowledge and info.name not in _KNOWLEDGE_COLLECTIONS:
            raise ValueError("Unreviewed FinanceKB knowledge tool")
        name = prefix + info.name
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
            raise ValueError("Invalid exposed MCP tool name")

        response_validator = None
        if financekb:
            assert info.output_schema is not None
            try:
                Draft202012Validator.check_schema(info.output_schema)
            except Exception:
                raise ValueError("FinanceKB output schema is invalid") from None
            response_validator = Draft202012Validator(info.output_schema)

        def fetch(
            args,
            original=info.name,
            response_validator=response_validator,
        ):
            return _fetch(
                client,
                original,
                args,
                response_validator=response_validator,
                financekb=financekb,
                knowledge=knowledge,
                domain=domain,
                redact_values=redact_values,
            )

        normalize = None
        if knowledge:
            normalize = partial(_knowledge_normalizer, tool_name=info.name)
        elif financekb:
            normalize = partial(_structured_normalizer, domain=domain)

        sources.append(
            SourceTool(
                name,
                server,
                info.name,
                reviewed.description if reviewed and reviewed.complete else info.description,
                reviewed.input_schema if reviewed and reviewed.complete else info.input_schema,
                fetch,
                schema_hash=approved[info.name],
                normalize=normalize,
                observed_schema_hash=info.schema_fingerprint,
            )
        )
    if set(approved) - {s.original_name for s in sources}:
        raise ValueError("Approved MCP tool is unavailable")
    return sources


async def _fetch(
    client,
    original,
    args,
    *,
    response_validator,
    financekb,
    knowledge,
    domain,
    redact_values,
):
    result = await client.call_tool(original, args)
    if result.is_error:
        raise SourceUnavailable("Source returned an error", code="tool_error")
    structured = result.structured
    if financekb:
        assert response_validator is not None
        if not isinstance(structured, dict):
            raise SourceUnavailable(
                "FinanceKB response does not match its approved output schema",
                code="contract_mismatch",
            )
        if knowledge:
            _knowledge_envelope(
                structured, tool_name=original, validator=response_validator
            )
        else:
            _structured_envelope(
                structured, domain=domain, validator=response_validator
            )
        if not knowledge and structured.get("available") is not True:
            # Keep the audit response; the normaliser registers no evidence.
            return redact(
                {
                    "structured": structured,
                    "text_blocks": list(result.text_blocks),
                    "unsupported_blocks": list(result.unsupported_blocks),
                },
                redact_values,
            )
    raw = {
        "structured": structured,
        "text_blocks": list(result.text_blocks),
        "unsupported_blocks": list(result.unsupported_blocks),
    }
    if not knowledge and (field := _FINANCEKB_PUBLICATION_FIELDS.get(original)):
        stamps = [row[field] for row in structured["items"] if row.get(field)]
        if stamps:
            try:
                raw["published_at"] = max(stamps, key=parse_research_as_of)
            except ValueError:
                raise SourceUnavailable(
                    "FinanceKB publication date is invalid"
                ) from None
    return redact(raw, redact_values)


class SourceUnavailable(RuntimeError):
    """A bounded source gap; ``code`` is a safe, model-facing category."""

    def __init__(
        self, message: str = "Source returned an error", *, code="source_unavailable"
    ):
        super().__init__(message)
        self.code = code


def build_search_tools(services, sources=()):
    tools = []
    for source in sources:
        if not source.name.startswith(_FINANCEKB_PREFIX) or not source.server.startswith(
            _FINANCEKB_PREFIX
        ):
            raise ValueError("Unapproved source")
        Draft202012Validator.check_schema(source.schema)
        if set(source.schema.get("properties", {})) & {"runtime", "_source"}:
            raise ValueError(
                "Source namespace or runtime-reserved parameters are invalid"
            )

        async def invoke(runtime: ToolRuntime[RunContext], _source=source, **arguments):
            await services.scope(runtime.context, "searcher")
            if set(key.lower() for key in arguments) & SECRET_FIELDS:
                raise PermissionError(
                    "Credentials must be supplied by the host in request headers"
                )
            errors = list(Draft202012Validator(_source.schema).iter_errors(arguments))
            if errors:
                return {
                    "ok": False,
                    "error": {
                        "code": "invalid_arguments",
                        "message": "Arguments do not match source schema",
                        "retryable": False,
                    },
                }
            return await services.acquire(
                runtime.context,
                runtime.tool_call_id,
                _source.server,
                _source.original_name,
                arguments,
                _source.fetch,
                kind=_source.kind,
                schema_hash=_source.schema_hash,
                normalize=_source.normalize,
            )

        # JSON Schema stays byte-for-byte equivalent to server discovery. Runtime is
        # explicitly injected, never part of the model schema.
        tools.append(
            StructuredTool(
                name=source.name,
                description=source.description,
                args_schema=source.schema,
                coroutine=invoke,
            )
        )
    return tools
