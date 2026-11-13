"""Extract an explicit run using verified references, never object ordering."""

import argparse
import hashlib
import json
from pathlib import Path
from research_copilot.contracts import ArtifactRef, RunContext
from research_copilot.runtime.acceptance import evaluate_acceptance
from research_copilot.subagents.services import digest


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def select_artifacts(state, *, review_id=None):
    reviews = state.role_data.get("reviews", {})
    if review_id:
        matches = [r for r in reviews.values() if r["review_id"] == review_id]
        if len(matches) != 1:
            raise ValueError("Exactly one explicit review is required")
        review = matches[0]
        ref = ArtifactRef.model_validate(review["reviewed"])
    else:
        ref = state.artifacts.get("report")
        if not ref:
            raise ValueError(
                "No current report; supply --review-id for historical extraction"
            )
        matches = [r for r in reviews.values() if r["reviewed"] == ref.model_dump()]
        if len(matches) > 1:
            raise ValueError("Ambiguous reviews; supply --review-id")
        review = matches[0] if matches else None

    def verified(reference, kind):
        obj = state.role_data.get("objects", {}).get(reference.artifact_id)
        if (
            not obj
            or obj["kind"] != kind
            or obj["ref"] != reference.model_dump()
            or digest(obj["payload"]) != reference.content_hash
        ):
            raise ValueError("Artifact identity/hash mismatch")
        return obj

    report = verified(ref, "report")
    analysis = verified(
        ArtifactRef.model_validate(report["payload"]["analysis_ref"]), "analysis"
    )
    return report, analysis, review


async def extract(args):
    if not args.db_authorized:
        raise ValueError("Database read requires explicit --db-authorized")
    from research_copilot.runtime.host_settings import HostSettings
    from research_copilot.runtime.storage import postgres_runtime

    settings = HostSettings()
    async with postgres_runtime(
        settings.database_url, initialize=False, read_only=True
    ) as (store, _):
        record = await store.load(args.report_id, args.actor_id)
        context = RunContext(
            record.actor_id,
            record.report_id,
            record.run_id,
            record.budget_id,
            record.policy_version,
        )
        state = await store.read_control(context)
        budget = await store.load_budget(context)
    report, analysis, review = select_artifacts(state, review_id=args.review_id)
    root = args.output or Path("output") / args.report_id / "extracted"
    root.mkdir(parents=True, exist_ok=True)
    save(root / "report_payload.json", report["payload"])
    save(root / "analysis_payload.json", analysis["payload"])
    save(root / "review.json", review)
    (root / "final_report.md").write_text(
        report["payload"]["document"], encoding="utf-8"
    )
    save(root / "acceptance.json", evaluate_acceptance(state, budget))
    save(
        root / "extraction.json",
        {
            "report_id": args.report_id,
            "status": record.status,
            "report_ref": report["ref"],
            "analysis_ref": analysis["ref"],
            "historical_extraction": state.artifacts.get("report")
            != ArtifactRef.model_validate(report["ref"]),
            "budget": budget.status(),
            "historical_availability": "unknown",
            "document_hash": hashlib.sha256(
                report["payload"]["document"].encode()
            ).hexdigest(),
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--actor-id", default="local-v2-e2e-operator")
    parser.add_argument("--review-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--db-authorized", action="store_true")
    from research_copilot.runtime.loop import run_async

    run_async(extract(parser.parse_args()))


if __name__ == "__main__":
    main()
