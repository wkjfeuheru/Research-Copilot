"""Explicit database/workspace/configuration binding, not a distributed lease."""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from sqlalchemy import CheckConstraint, Column, Integer, MetaData, String, Table, Text
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import make_url

from research_copilot.runtime.backup import _unlinked, workspace_lease
from research_copilot.runtime.run_queue import RunQueue, UNFINISHED, _clock, run_jobs
from research_copilot.workspace import safe_component

metadata = MetaData()
host_binding = Table(
    "research_host_binding",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("workspace_id", String(32), nullable=False),
    Column("workspace_path", Text, nullable=False),
    Column("database_identity", String(64), nullable=False),
    Column("config_digest", String(64), nullable=False),
    Column("revision", Integer, nullable=False),
    Column("updated_at", String(64), nullable=False),
    CheckConstraint("id = 1 AND revision > 0"),
)
binding_events = Table(
    "research_host_binding_events",
    metadata,
    Column("revision", Integer, primary_key=True),
    Column("actor_id", String(128), nullable=False),
    Column("note", Text, nullable=False),
    Column("previous_digest", String(64)),
    Column("config_digest", String(64), nullable=False),
    Column("previous_workspace", Text),
    Column("workspace_path", Text, nullable=False),
    Column("at", String(64), nullable=False),
)
MARKER = ".research-copilot-host.json"


def digest(configuration):
    return sha256(
        json.dumps(
            configuration, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def database_identity(engine):
    return database_identity_url(engine.url)


def database_identity_url(url):
    url = make_url(url)
    # Credentials and connection options do not identify the data. Operators
    # must use the same canonical endpoint spelling across cooperating hosts.
    return digest(
        [
            url.get_backend_name(),
            (url.host or "").lower(),
            url.port or 5432,
            url.database,
        ]
    )


def _path(root):
    return os.path.normcase(str(_unlinked(root)))


def _marker(root):
    path = Path(root) / MARKER
    if path.is_symlink() or path.is_junction():
        raise ValueError("Invalid linked host marker")
    if not path.exists():
        return None
    try:
        if path.stat().st_nlink != 1 or path.stat().st_size > 4096:
            raise ValueError()
        value = json.loads(path.read_text("utf-8"))
        if (
            set(value) != {"format", "workspace_id", "database_identity"}
            or value["format"] != 1
        ):
            raise ValueError()
        if not re.fullmatch(r"[a-f0-9]{32}", value["workspace_id"]) or not re.fullmatch(
            r"[a-f0-9]{64}", value["database_identity"]
        ):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError(
            "Invalid host marker; inspect the preserved workspace"
        ) from None


async def check_host(store, root, config_digest):
    async with store.engine.connect() as connection:
        row = (await connection.execute(select(host_binding))).mappings().one_or_none()
    if row is None:
        raise ValueError("Host is unbound; stop writers and run rc bind-host")
    if row["config_digest"] != config_digest:
        raise ValueError(
            "Host binding mismatch; inspect configuration and workspace before explicit replacement"
        )
    check_workspace_pair(row, root, database_identity(store.engine))
    return dict(row)


def check_workspace_pair(row, root, identity):
    """Validate source identity; only an unmarked workspace may be unbound."""
    path = _path(root)
    marker = _marker(root)
    if row is None:
        if marker is not None:
            raise ValueError("Host marker exists but the database binding is missing")
        return
    if row["workspace_path"] != path or row["database_identity"] != identity:
        raise ValueError(
            "Host binding mismatch; inspect database and workspace pairing"
        )
    if marker != {
        "format": 1,
        "workspace_id": row["workspace_id"],
        "database_identity": row["database_identity"],
    }:
        raise ValueError(
            "Host marker missing or mismatched; run rc bind-host after inspection"
        )


async def bind_host(store, root, config_digest, *, actor_id, note, replace=False):
    if (
        not re.fullmatch(r"[a-f0-9]{64}", config_digest)
        or not isinstance(note, str)
        or not 1 <= len(note.strip()) <= 2000
    ):
        raise ValueError("A configuration digest and binding evidence are required")
    safe_component(actor_id)
    if len(actor_id) > 128 or type(replace) is not bool:
        raise ValueError("Invalid binding operation")
    from research_copilot.runtime.experience import retrospective_jobs

    with workspace_lease(root) as root:
        path, identity = _path(root), database_identity(store.engine)
        async with RunQueue(store.engine).transaction() as connection:
            previous = (
                (await connection.execute(select(host_binding)))
                .mappings()
                .one_or_none()
            )
            marker = _marker(root)
            matches = previous is not None and all(
                previous[key] == value
                for key, value in (
                    ("workspace_path", path),
                    ("database_identity", identity),
                    ("config_digest", config_digest),
                )
            )
            expected_marker = (
                {
                    "format": 1,
                    "workspace_id": previous["workspace_id"],
                    "database_identity": identity,
                }
                if previous
                else marker
            )
            if matches and marker == expected_marker:
                return dict(previous)
            if previous and not replace:
                raise ValueError(
                    "Host binding change requires explicit --replace and evidence"
                )
            if (
                previous
                and path != previous["workspace_path"]
                and (not marker or marker["workspace_id"] != previous["workspace_id"])
            ):
                raise ValueError(
                    "Replacement workspace must retain the matching backup marker"
                )
            if marker and (
                (previous and marker["workspace_id"] != previous["workspace_id"])
                or marker["database_identity"]
                not in {
                    identity,
                    previous["database_identity"] if previous else identity,
                }
            ):
                raise ValueError("Host marker belongs to another database/workspace")
            unfinished = (
                await connection.execute(
                    select(func.count())
                    .select_from(run_jobs)
                    .where(run_jobs.c.status.in_(UNFINISHED))
                )
            ).scalar_one()
            retro = (
                await connection.execute(
                    select(func.count())
                    .select_from(retrospective_jobs)
                    .where(retrospective_jobs.c.status.in_(("pending", "running")))
                )
            ).scalar_one()
            if unfinished or retro:
                raise ValueError(
                    "Cannot bind with unfinished research or retrospective work; stop and settle writers first"
                )
            now = (await _clock(connection)).isoformat()
            value = {
                "id": 1,
                "workspace_id": previous["workspace_id"]
                if previous
                else marker["workspace_id"]
                if marker
                else uuid4().hex,
                "workspace_path": path,
                "database_identity": identity,
                "config_digest": config_digest,
                "revision": previous["revision"] + 1 if previous else 1,
                "updated_at": now,
            }
            root.mkdir(parents=True, exist_ok=True)
            target = root / MARKER
            temporary = root / (MARKER + "." + uuid4().hex + ".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "format": 1,
                        "workspace_id": value["workspace_id"],
                        "database_identity": identity,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, target)
            if previous:
                await connection.execute(
                    update(host_binding).where(host_binding.c.id == 1).values(**value)
                )
            else:
                await connection.execute(insert(host_binding).values(**value))
            await connection.execute(
                insert(binding_events).values(
                    revision=value["revision"],
                    actor_id=actor_id,
                    note=note.strip(),
                    previous_digest=previous["config_digest"] if previous else None,
                    config_digest=config_digest,
                    previous_workspace=previous["workspace_path"] if previous else None,
                    workspace_path=path,
                    at=now,
                )
            )
            return value
