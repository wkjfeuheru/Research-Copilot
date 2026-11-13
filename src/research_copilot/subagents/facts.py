"""Exact source locations and handoff facts; no evidence registration or calculations."""

import json
import re
from research_copilot.subagents.contracts import BusinessInputError


def document(raw):
    return (
        raw["structured"]
        if isinstance(raw, dict) and raw.get("structured") is not None
        else raw
    )


def iter_source_segments(raw):
    def visit(value, pointer):
        if isinstance(value, dict):
            for key, child in value.items():
                yield from visit(
                    child,
                    pointer + "/" + str(key).replace("~", "~0").replace("/", "~1"),
                )
        elif isinstance(value, list):
            for i, child in enumerate(value):
                yield from visit(child, pointer + "/" + str(i))
        elif isinstance(value, str):
            for start in range(0, len(value), 208):
                yield {
                    "pointer": pointer,
                    "start": start,
                    "end": min(start + 240, len(value)),
                    "text": value[start : start + 240],
                }
        else:
            yield {"pointer": pointer, "value": value}

    if not (isinstance(raw, dict) and raw.get("encoding") == "base64"):
        yield from visit(document(raw), "")


def source_segments(raw):
    return (
        "\n".join(
            json.dumps(row, ensure_ascii=False) for row in iter_source_segments(raw)
        )
        + "\n"
    ).encode()


def validate_source_metadata(raw, pointer, *, currency, basis):
    """Numeric strings still inherit explicit metadata from their enclosing record."""
    value, metadata = document(raw), {}
    for part in pointer.removeprefix("/").split("/") if pointer else ():
        if isinstance(value, dict):
            metadata.update(
                {k: value[k] for k in ("currency", "reporting_scope") if k in value}
            )
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    if "currency" in metadata:
        disclosed = metadata["currency"]
        if (
            disclosed is None
            and currency.casefold() not in {"unknown", "未知", "未披露"}
        ) or (disclosed is not None and currency != str(disclosed)):
            raise ValueError(
                "currency differs from source; null must remain unknown, not an assumed currency"
            )
    if "reporting_scope" in metadata and metadata["reporting_scope"] is None:
        unknown = r"unknown|未知|未标明|未披露|未标注|不明"
        clauses = re.split(r"[，,；;。]", basis)
        if not re.search(unknown, basis, re.I) or any(
            re.search(r"合并|母公司|consolidated|parent", clause, re.I)
            and not re.search(unknown, clause, re.I)
            for clause in clauses
        ):
            raise ValueError(
                "reporting_scope is null: retain reporting_scope=unknown in basis; remove unverified consolidated/parent assertions"
            )


def _occurrence_windows(value, needle, limit=2, pad=70):
    """Exact source windows around each occurrence, for a copy-ready repair."""
    windows, start = [], 0
    while len(windows) < limit and needle:
        at = value.find(needle, start)
        if at < 0:
            break
        windows.append(
            value[max(0, at - pad) : min(len(value), at + len(needle) + pad)]
        )
        start = at + len(needle)
    return windows


def validate_facts(result, state, scope):
    from research_copilot.subagents.services import digest

    brief = scope.get("task_brief") or {}
    if len(result.facts) > brief.get("max_facts", 15):
        raise BusinessInputError(
            "Fact count exceeds frozen max_facts; split the task explicitly"
        )
    fields = set(brief.get("required_fields", [])) | set(
        brief.get("optional_fields", [])
    )
    if fields and any(f.key not in fields for f in result.facts):
        raise BusinessInputError(
            "Fact outside frozen required/optional fields. "
            f"Frozen fields: {sorted(fields)}. Widen optional_fields via a "
            "bounded repair dispatch to register qualitative facts; "
            "do not drop required content to fit the field list."
        )
    facts, keys, problems = [], set(), []
    status = getattr(result, "status", "completed")
    for fact in result.facts:
        if fact.key in keys:
            raise BusinessInputError("Duplicate fact key")
        keys.add(fact.key)
        if fact.evidence_id not in scope["evidence_ids"]:
            raise BusinessInputError(
                f"Fact {fact.key}: evidence_id {fact.evidence_id!r} is outside this "
                f"dispatch. Authorized evidence IDs: {sorted(scope['evidence_ids'])}."
            )
        if fact.evidence_id not in result.evidence_ids:
            raise BusinessInputError(
                f"Fact {fact.key}: evidence_id {fact.evidence_id!r} must also be "
                "listed in SearchResult.evidence_ids."
            )
        evidence = state.role_data["evidence"][fact.evidence_id]
        if (
            fact.content_hash != evidence["content_hash"]
            or digest(evidence["raw"]) != fact.content_hash
        ):
            raise ValueError("Fact evidence hash mismatch")
        value = document(evidence["raw"])
        try:
            if fact.pointer:
                if not fact.pointer.startswith("/"):
                    raise ValueError("pointer")
                for part in fact.pointer[1:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    if isinstance(value, list) and not part.isdecimal():
                        raise ValueError("index")
                    value = value[int(part)] if isinstance(value, list) else value[part]
        except (KeyError, TypeError, IndexError, ValueError):
            from research_copilot.subagents.source_context import leaf_pointers

            problems.append(
                f"Fact {fact.key}: pointer {fact.pointer!r} does not resolve. "
                f"Use source-root pointers (not projection wrappers): "
                f"{leaf_pointers(document(evidence['raw']))}. "
                "Correct the pointer; preserve the source hash and exact quote."
            )
            continue
        try:
            validate_source_metadata(
                evidence["raw"], fact.pointer, currency=fact.currency, basis=fact.basis
            )
        except ValueError as exc:
            problems.append(f"Fact {fact.key}: {exc}")
            continue
        ref = fact.model_dump(exclude={"quote"})
        if isinstance(value, str):
            occurrences = value.count(fact.quote) if fact.quote else 0
            in_quote = fact.quote.count(fact.raw_value)
            if occurrences >= 1 and in_quote == 1:
                # The same reporting row may appear in several tables of one
                # disclosure. Any occurrence supports the same value; use the
                # first one as the auditable span.
                start = value.index(fact.quote) + fact.quote.index(fact.raw_value)
                end = start + len(fact.raw_value)
                if occurrences > 1:
                    fact = fact.model_copy(
                        update={
                            "quote": value[
                                max(0, start - 70) : min(len(value), end + 70)
                            ]
                        }
                    )
            elif status != "completed" and value.count(fact.raw_value) == 1:
                # A partial/blocked submission may cite a malformed quote when the
                # raw value is unique at this pointer; derive the exact span.
                start = value.index(fact.raw_value)
                end = start + len(fact.raw_value)
                fact = fact.model_copy(
                    update={
                        "quote": value[max(0, start - 70) : min(len(value), end + 70)]
                    }
                )
            else:
                windows = _occurrence_windows(value, fact.raw_value)
                problems.append(
                    f"Fact {fact.key}: quote/value must locate one verifiable source span: "
                    f"evidence_id={fact.evidence_id}, pointer={fact.pointer}, "
                    f"raw_value={fact.raw_value!r}, quote_occurrences={occurrences}, "
                    f"raw_value_in_quote={in_quote}. "
                    "The quote must appear in the source and contain raw_value exactly once; "
                    "the value occurs more than once or the quote does not contain it verbatim. "
                    f"Copy one of these exact source windows as quote (raw_value appears once in each): "
                    f"{windows}. Keep raw_value as the exact source token. "
                    "Omit unverifiable facts and record a gap."
                )
                continue
            if re.fullmatch(r"[+-]?\d[\d,]*(?:\.\d+)?%?", fact.raw_value) and (
                re.search(r"[\d+\-]$|\d[.,]$", value[:start])
                or re.match(r"\d|[.,]\d|%|[eE][+-]?\d", value[end:])
            ):
                problems.append(
                    f"Fact {fact.key} extraction splits a numeric source token: "
                    f"raw_value={fact.raw_value!r}, quote={fact.quote[:120]!r}; "
                    "keep the percent sign in raw_value (for example 77.05%) or "
                    "quote a span where the number is not adjacent to %/digit/./,"
                )
                continue
            ref.update(
                text_start=start,
                text_end=start + len(fact.raw_value),
                extraction="text-extraction",
            )
        elif (
            isinstance(value, (dict, list, bool))
            or value is None
            or str(value) != fact.raw_value
        ):
            problems.append(
                f"Fact {fact.key}: raw_value differs from original; "
                "supply an exact unique quote for text extraction"
            )
            continue
        ref["transformation"] = "identity"
        raw = evidence["raw"] if isinstance(evidence["raw"], dict) else {}
        facts.append(
            {
                "input_ref": ref,
                "quote": fact.quote,
                "verification": "source-content-match-only",
                "source": {
                    "server": evidence["source"]["server"],
                    "title": raw.get("title"),
                    "url": raw.get("url"),
                    "published_at": raw.get("published_at"),
                    "collected_at": evidence["collected_at"],
                    "kind": evidence["kind"],
                },
            }
        )
    missing = set(brief.get("required_fields", [])) - keys
    if status == "completed" and missing:
        problems.append("Missing required facts: " + ", ".join(sorted(missing)))
    stop_reason = getattr(result, "stop_reason", None)
    if brief.get("required_fields") and not stop_reason:
        problems.append(
            "Return stop_reason and gaps for missing required/optional fields"
        )
    if stop_reason == "required_complete" and missing:
        problems.append("required_complete cannot hide missing required fields")
    if problems:
        raise BusinessInputError(
            f"Fact submission rejected with {len(problems)} problem(s): "
            + " | ".join(problems)
        )
    return facts
