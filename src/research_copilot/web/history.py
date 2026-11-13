"""Model-free, database-enforced read-only workbench dependencies."""

from contextlib import asynccontextmanager
from sqlalchemy import select

from research_copilot.runtime.backup import workspace_lease
from research_copilot.runtime.host_binding import (
    check_workspace_pair,
    database_identity,
    host_binding,
)
from research_copilot.runtime.storage import postgres_runtime
from research_copilot.runtime.business import BusinessCoordinator
from research_copilot.runtime.session import SessionService
from research_copilot.runtime.experience import ExperienceService
from research_copilot.subagents.services import RoleServices


@asynccontextmanager
async def open_history(settings, *, workspace_root, policy_version):
    if not settings.database_url:
        raise ValueError("RC_DATABASE_URL is required")
    with workspace_lease(workspace_root, shared=True):
        async with postgres_runtime(settings.database_url, read_only=True) as (
            store,
            _,
        ):
            async with store.engine.connect() as connection:
                row = (
                    (await connection.execute(select(host_binding)))
                    .mappings()
                    .one_or_none()
                )
            check_workspace_pair(row, workspace_root, database_identity(store.engine))

            def forbidden(*args):
                raise PermissionError(
                    "History-only server cannot construct an execution graph"
                )

            business = BusinessCoordinator(store)
            services = RoleServices(
                repository=store,
                business=business,
                workspace_root=workspace_root,
                experience=ExperienceService(store) if store.experience_ready else None,
            )
            yield (
                SessionService(
                    store=store, agent_factory=forbidden, policy_version=policy_version
                ),
                store,
                services,
            )
