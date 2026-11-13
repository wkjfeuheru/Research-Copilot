"""Discovery readiness and admission, independent of model construction."""

from dataclasses import asdict


class SourcesUnavailable(RuntimeError):
    code = "sources_unavailable"


def source_health(statuses, checked_at=None):
    # None is reserved for non-FinanceKB hosts (for example isolated test hosts).
    # Production always supplies an explicit tuple; an empty tuple is unavailable.
    domains = [asdict(status) for status in statuses or ()]
    available = sum(status["available"] for status in domains)
    ready = statuses is None or available > 0
    return {
        "ready": ready,
        "available_domains": available,
        "total_domains": len(domains),
        "checked_at": checked_at,
        "domains": domains,
        "detail": ""
        if ready
        else (
            "数据源尚未就绪，当前没有可用的数据域，研究尚未启动。"
            "请检查来源连接与合同；恢复后重启研究服务以重新连接，再重试。"
        ),
    }


def require_available_sources(statuses):
    state = source_health(statuses)
    if not state["ready"]:
        raise SourcesUnavailable(state["detail"])
