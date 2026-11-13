"""Read-only, bounded diagnostics from durable metadata, never request bodies."""


def diagnostics(state, budget):
    sources = state.role_data.get("source_audit", [])
    models = state.role_data.get("model_audit", [])
    settled = [event for event in models if event.get("event") == "settled"]
    return {
        "source_calls": len(sources),
        "source_failures": sum(row.get("status") == "failed" for row in sources),
        "source_duration_ms": sum(row.get("duration_ms", 0) for row in sources),
        "model_duration_seconds": sum(row.get("elapsed_seconds", 0) for row in settled),
        "model_failures": sum(bool(row.get("error_type")) for row in settled),
        "admission_rejections": sum(
            row.get("event") == "admission_rejected" for row in models
        ),
        "unknown_requests": budget.status()["unknown_requests"],
        "sources": sources[-50:],
        "models": models[-50:],
    }
