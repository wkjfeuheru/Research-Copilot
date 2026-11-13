"""Application lifespan for a session. The host owns models, database, and sources."""

from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from research_copilot.llm.config import load_models_config, open_models
from research_copilot.orchestration.factory import SupervisorFactory
from research_copilot.runtime.business import BusinessCoordinator
from research_copilot.runtime.host_settings import HostSettings
from research_copilot.runtime.session import SessionService
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.sources.financekb import SourceStatus, open_financekb_sources
from research_copilot.sources.financekb_contracts import APPROVED_SCHEMAS, CONTRACT_SHA256
from research_copilot.subagents.services import RoleServices
from research_copilot.workspace import bundled_resources


async def initialize_database(settings: HostSettings) -> None:
    if not settings.database_url:
        raise ValueError("RC_DATABASE_URL is required")
    async with postgres_runtime(settings.database_url, initialize=True):
        return


def prepare_host_configuration(
    settings,
    *,
    models_path,
    policy_version,
    mode="production",
    rules_path=Path("config/compliance_rules.yaml"),
    rules_approval_path=None,
    execution_policy_path=Path("config/execution_policy.toml"),
):
    """Load the exact runtime inputs once; never include credential values."""
    from research_copilot import __version__
    from research_copilot.runtime.host_binding import digest
    from research_copilot.runtime.identity import RUNTIME_VERSION
    from research_copilot.runtime.preflight import preflight
    from research_copilot.sandbox.config import SandboxConfig
    from research_copilot.subagents.searcher import NORMALIZATION_VERSION

    config = load_models_config(Path(models_path))
    rules, policy_check = preflight(
        rules_path=rules_path,
        mode=mode,
        approval_path=rules_approval_path,
        policy_path=execution_policy_path,
    )
    sandbox_config = SandboxConfig.load()
    search = {
        "financekb": {
            "contract": CONTRACT_SHA256,
            "schemas": APPROVED_SCHEMAS,
            "normalization_version": NORMALIZATION_VERSION,
        }
    }
    if settings.financekb_base_url:
        search["financekb"]["base_url"] = settings.financekb_base_url
    fingerprint = digest(
        {
            "format": 2,
            "application_version": __version__,
            "runtime_version": RUNTIME_VERSION,
            "policy_version": policy_version,
            "models": config.model_dump(mode="json"),
            "rules": rules,
            "preflight": policy_check,
            "sandbox": sandbox_config.model_dump(mode="json"),
            "search": search,
        }
    )
    return config, rules, policy_check, fingerprint, sandbox_config


async def bind_host_configuration(
    settings,
    *,
    workspace_root,
    actor_id,
    note,
    replace=False,
    **configuration,
):
    """Explicit operator registration; does not construct or call model clients."""
    from research_copilot.runtime.host_binding import bind_host

    if not settings.database_url:
        raise ValueError("RC_DATABASE_URL is required")
    _, _, _, fingerprint, _ = prepare_host_configuration(settings, **configuration)
    async with postgres_runtime(settings.database_url, maintenance=True) as (store, _):
        return await bind_host(
            store,
            workspace_root,
            fingerprint,
            actor_id=actor_id,
            note=note,
            replace=replace,
        )


@asynccontextmanager
async def _open_session(
    settings: HostSettings,
    *,
    models_path: Path,
    workspace_root: Path,
    policy_version: str,
    initialize: bool = False,
    mode: str = "production",
    rules_path: Path = Path("config/compliance_rules.yaml"),
    rules_approval_path: Path | None = None,
    execution_policy_path: Path = Path("config/execution_policy.toml"),
):
    if not settings.database_url:
        raise ValueError("RC_DATABASE_URL is required")
    from research_copilot.runtime.host_binding import check_host

    config, rules, policy_check, fingerprint, sandbox_config = (
        prepare_host_configuration(
            settings,
            models_path=models_path,
            policy_version=policy_version,
            rules_path=rules_path,
            mode=mode,
            rules_approval_path=rules_approval_path,
            execution_policy_path=execution_policy_path,
        )
    )
    async with postgres_runtime(settings.database_url, initialize=initialize) as (
        store,
        checkpointer,
    ):
        await check_host(store, workspace_root, fingerprint)
        credentials = {}
        if settings.deepseek_api_key is not None:
            credentials["RC_DEEPSEEK_API_KEY"] = (
                settings.deepseek_api_key.get_secret_value()
            )
        async with (
            open_models(config, credentials=credentials) as models,
            AsyncExitStack() as source_lifetimes,
        ):
            source_tools: tuple = ()
            source_note = ""
            source_catalog = {}
            source_contract = {}
            source_statuses: tuple[SourceStatus, ...] = ()
            if settings.financekb_base_url and settings.financekb_api_token:
                bundle = await source_lifetimes.enter_async_context(
                    open_financekb_sources(
                        settings.financekb_base_url,
                        settings.financekb_api_token.get_secret_value(),
                        ca_bundle=settings.financekb_ca_bundle,
                        call_timeout=settings.financekb_call_timeout_seconds,
                    )
                )
                source_tools = bundle.tools
                source_note = bundle.note
                source_catalog = bundle.catalog
                source_contract = bundle.contract
                source_statuses = bundle.statuses
            from datetime import datetime, timezone

            source_checked_at = datetime.now(timezone.utc).isoformat()
            business = BusinessCoordinator(store)
            store.resume_validator = business.validate_resume
            experience = None
            extractor = None
            if store.experience_ready:
                from research_copilot.runtime.experience import (
                    ExperienceService,
                    LLMExperienceExtractor,
                )

                experience = ExperienceService(store)
                model = models.get("summarizer") or models.get("reviewer")
                if model is not None:
                    extractor = LLMExperienceExtractor(model)
            services = RoleServices(
                repository=store,
                workspace_root=Path(workspace_root),
                business=business,
                report_rules=rules,
                experience=experience,
            )
            services.experience_extractor = extractor
            services.preflight_result = policy_check
            factory = SupervisorFactory(
                models=models,
                store=store,
                business=business,
                checkpointer=checkpointer,
                workspace_root=Path(workspace_root),
                resources_root=bundled_resources(),
                role_services=services,
                source_tools=source_tools,
                source_note=source_note,
                source_catalog=source_catalog,
                source_contract=source_contract,
                sandbox_config=sandbox_config,
            )
            session = SessionService(
                store=store,
                agent_factory=factory,
                policy_version=policy_version,
                source_statuses=source_statuses,
                source_checked_at=source_checked_at,
            )
            yield session, store, services


@asynccontextmanager
async def open_session(settings, *, workspace_root, **kwargs):
    from research_copilot.runtime.backup import workspace_lease

    with workspace_lease(workspace_root, shared=not kwargs.get("initialize", False)):
        async with _open_session(
            settings, workspace_root=workspace_root, **kwargs
        ) as session:
            yield session
