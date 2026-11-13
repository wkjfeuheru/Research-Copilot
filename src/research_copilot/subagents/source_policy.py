"""Research time and source eligibility, independent of collection time."""

from datetime import datetime, time, timezone, timedelta
import json
import re

SHANGHAI = timezone(timedelta(hours=8))


def parse_research_as_of(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Research as_of is required")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.combine(
            datetime.fromisoformat(value).date(), time.max, SHANGHAI
        )
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp as_of requires a timezone")
    return parsed.astimezone(SHANGHAI)


BODY_DATE = re.compile(
    r"(?:公告日期|发布日期|发布时间|披露日期|全文披露日期|published(?:\s*at)?)"
    r"\s*[:：]?\s*((?:19|20)\d{2})\s*[-年]\s*(\d{1,2})\s*[-月]\s*(\d{1,2})\s*日?"
)


def body_publication_date(record):
    """A source page that explicitly labels its publication date is usable."""
    if record.get("kind") not in {"search_snippet", "original_text"}:
        return None
    raw = record.get("raw", {})
    body = raw.get("body") if isinstance(raw, dict) else None
    if not isinstance(body, str):
        return None
    match = BODY_DATE.search(body)
    if not match:
        return None
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def assess_source(record, as_of, *, publication=None):
    boundary = parse_research_as_of(as_of)
    raw = record.get("raw", {})
    raw = raw if isinstance(raw, dict) else {}
    stamp = raw.get("published_at") or body_publication_date(record)
    # Publication attestations are supplied only by the host after checking the
    # issuer/exchange listing. They never change the frozen raw document.
    if (
        not stamp
        and publication
        and record.get("content_hash")
        and publication.get("content_hash") == record["content_hash"]
        and publication.get("verified_by")
        and str(publication.get("official_url", "")).startswith("https://")
    ):
        stamp = publication.get("published_at")
    result = {
        "eligible": False,
        "historical_availability": record.get("source", {}).get(
            "historical_availability", "unknown"
        ),
        "published_at": stamp,
        "reason": "publication_date_unknown",
    }
    if stamp:
        try:
            published = parse_research_as_of(stamp)
        except ValueError:
            result["reason"] = "publication_date_invalid"
            return result
        if published > boundary:
            result["reason"] = "published_after_as_of"
            return result
        if raw.get("qualification", record.get("source_qualification", {})).get(
            "relevant"
        ) is not True and record.get("kind") in {"search_snippet", "original_text"}:
            result["reason"] = "irrelevant_source"
            return result
        return {**result, "eligible": True, "reason": "publication_within_boundary"}
    if record.get("kind") in {"attachment_original", "financial_data"}:
        return {**result, "eligible": True, "reason": "controlled_input_pit_unknown"}
    return result


def qualify_text(text, brief):
    groups = [brief.get("entity_terms", []), brief.get("topic_terms", [])]
    matched = [
        [term for term in group if term.casefold() in text.casefold()]
        for group in groups
    ]
    return {
        "relevant": all(matched),
        "matched_entity_terms": matched[0],
        "matched_topic_terms": matched[1],
        "method": "frozen-task-term-match-v1",
    }


def parse_task_brief(description):
    from research_copilot.subagents.contracts import TaskBrief

    objects, index = [], 0
    decoder = json.JSONDecoder()
    while index < len(description):
        start = description.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(description[start:])
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid or truncated TaskBrief JSON") from exc
        if isinstance(value, dict) and "task_id" in value:
            objects.append(value)
        index = start + end
    if len(objects) > 1:
        raise ValueError("Ambiguous TaskBrief objects")
    if not objects:
        return None
    return TaskBrief.model_validate(objects[0])
