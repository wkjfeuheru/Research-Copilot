"""PostgreSQL checkpointer recovery after the server drops its connections.

Run explicitly::

    RC_TEST_DATABASE_URL=postgresql://... pytest -q -m db tests/integration/test_checkpoint_postgres.py

The pool must replace dead connections; otherwise the first checkpoint read
after a PostgreSQL restart fails with an OperationalError.
"""

import os
from uuid import uuid4

import psycopg
import pytest

from research_copilot.runtime.identity import checkpoint_thread_id
from research_copilot.runtime.urls import to_checkpoint_url

pytestmark = pytest.mark.db


def _url():
    url = os.environ.get("RC_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("需要 RC_TEST_DATABASE_URL 指向本地 PostgreSQL")
    return url


@pytest.mark.asyncio
async def test_checkpoint_read_recovers_after_connections_are_terminated():
    from research_copilot.runtime.storage import postgres_runtime

    url = _url()
    config = {
        "configurable": {"thread_id": checkpoint_thread_id("pg-drop-" + uuid4().hex)}
    }
    async with postgres_runtime(url, initialize=True) as (_store, checkpointer):
        async with await psycopg.AsyncConnection.connect(
            to_checkpoint_url(url), autocommit=True
        ) as admin:
            killed = await admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where application_name = 'research-copilot-checkpoint' "
                "and pid <> pg_backend_pid()"
            )
            assert await killed.fetchall()
        assert await checkpointer.aget_tuple(config) is None
