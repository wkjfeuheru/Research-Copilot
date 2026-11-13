"""Offline joint PostgreSQL/workspace backup. Never overwrites a restore target."""

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from research_copilot.runtime.migrations import SCHEMA_VERSION
from research_copilot.runtime.maintenance import maintenance_connection
from research_copilot.runtime.urls import to_checkpoint_url
from research_copilot.runtime.workspace_lock import workspace_lease


def _unlinked(path):
    path = Path(path).absolute()
    if any(part.is_symlink() or part.is_junction() for part in [path, *path.parents]):
        raise ValueError("Linked backup/workspace paths are not supported")
    return path.resolve()


def file_inventory(root, *, exclude_manifest=False):
    root = _unlinked(root)
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Linked backup/workspace entries are not supported")
        if path.is_file() and not (exclude_manifest and path == root / "manifest.json"):
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            result[path.relative_to(root).as_posix()] = {
                "sha256": digest,
                "bytes": path.stat().st_size,
            }
    return result


def verify_bundle(root):
    root = _unlinked(root)
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    if manifest.get("format") != 1 or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            "Unsupported backup format/schema; use the matching application"
        )
    if manifest["files"] != file_inventory(root, exclude_manifest=True):
        raise ValueError("Backup file inventory mismatch")
    if "database.dump" not in manifest["files"] or not (root / "workspace").is_dir():
        raise ValueError("Incomplete joint backup")
    return manifest


def _tables(connection):
    return [
        row[0]
        for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()
    ]


def _json_default(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes_hex": bytes(value).hex()}
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def database_inventory(connection):
    result = {}
    for table in _tables(connection):
        cursor = connection.execute(
            sql.SQL("SELECT * FROM public.{}").format(sql.Identifier(table))
        )
        columns = [column.name for column in cursor.description]
        rows = sorted(
            json.dumps(
                dict(zip(columns, row)),
                sort_keys=True,
                default=_json_default,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for row in cursor.fetchall()
        )
        result[table] = {
            "rows": len(rows),
            "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest(),
        }
    return result


def _validate_links(connection):
    records = connection.execute(
        "SELECT report_id,budget_id,budget FROM sessions"
    ).fetchall()
    report_ids = {row[0] for row in records}
    if any(budget_id != budget.get("budget_id") for _, budget_id, budget in records):
        raise ValueError("Session/budget identity mismatch")
    if "checkpoints" in _tables(connection):
        for (thread_id,) in connection.execute(
            "SELECT DISTINCT thread_id FROM checkpoints"
        ):
            if thread_id.startswith("v2:") and thread_id[3:] not in report_ids:
                raise ValueError("Checkpoint references a missing session")
    versions = [
        row[0]
        for row in connection.execute(
            "SELECT version FROM rc_schema_migrations ORDER BY version"
        )
    ]
    if versions != list(range(1, SCHEMA_VERSION + 1)):
        raise ValueError("Backup requires the current explicitly migrated schema")


def _pg_tool(arguments, database_url):
    parameters = psycopg.conninfo.conninfo_to_dict(to_checkpoint_url(database_url))
    names = {
        "dbname": "PGDATABASE",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "host": "PGHOST",
        "hostaddr": "PGHOSTADDR",
        "port": "PGPORT",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
        "connect_timeout": "PGCONNECT_TIMEOUT",
        "options": "PGOPTIONS",
        "application_name": "PGAPPNAME",
    }
    if set(parameters) - names.keys():
        raise ValueError("Unsupported libpq backup connection options")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env.update({names[key]: value for key, value in parameters.items()})
    if arguments[0] == "pg_restore":
        arguments = [arguments[0], "--dbname=" + parameters["dbname"], *arguments[1:]]
    # Credentials stay out of argv, manifest and error output. No shell,
    # automatic retry or cleanup.
    result = subprocess.run(
        arguments, env=env, capture_output=True, timeout=1800, check=False
    )
    if result.returncode:
        from urllib.parse import quote

        diagnosis = result.stderr.decode("utf-8", errors="replace")
        password = parameters.get("password")
        if password:
            diagnosis = diagnosis.replace(password, "[redacted]").replace(
                quote(password, safe=""), "[redacted]"
            )
        raise RuntimeError(
            f"{arguments[0]} failed (exit {result.returncode}); partial target retained: {diagnosis[:4000]}"
        )


def backup(database_url, workspace_root, destination):
    # host_binding uses the workspace lock helpers in this module.
    from research_copilot.runtime.host_binding import (
        check_workspace_pair,
        database_identity_url,
    )

    destination = _unlinked(destination)
    with workspace_lease(workspace_root) as workspace:
        if (
            not workspace.is_dir()
            or destination.is_relative_to(workspace)
            or workspace.is_relative_to(destination)
        ):
            raise ValueError(
                "Backup needs an existing workspace and a separate new destination"
            )
        file_inventory(workspace)  # Reject links before invoking any database tool.
        with maintenance_connection(database_url, operation="backup"):
            with psycopg.connect(to_checkpoint_url(database_url)) as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                connection.execute("SET LOCAL lock_timeout = '5s'")
                connection.execute("SELECT pg_advisory_xact_lock(725231109)")
                tables = _tables(connection)
                if not tables:
                    raise ValueError("Cannot back up an empty database")
                connection.execute(
                    sql.SQL("LOCK TABLE {} IN SHARE MODE").format(
                        sql.SQL(",").join(
                            sql.Identifier("public", table) for table in tables
                        )
                    )
                )
                _validate_links(connection)
                with connection.cursor(row_factory=dict_row) as cursor:
                    row = cursor.execute(
                        "SELECT * FROM public.research_host_binding WHERE id = 1"
                    ).fetchone()
                check_workspace_pair(
                    row, workspace, database_identity_url(database_url)
                )
                destination.mkdir(parents=True, exist_ok=False)
                snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[
                    0
                ]
                inventory = database_inventory(connection)
                _pg_tool(
                    [
                        "pg_dump",
                        "--format=custom",
                        "--no-owner",
                        "--no-acl",
                        "--schema=public",
                        "--snapshot=" + snapshot,
                        "--file=" + str(destination / "database.dump"),
                    ],
                    database_url,
                )
                shutil.copytree(workspace, destination / "workspace")
                if file_inventory(workspace) != file_inventory(
                    destination / "workspace"
                ):
                    raise ValueError(
                        "Workspace changed during backup; incomplete bundle retained"
                    )
            manifest = {
                "format": 1,
                "schema_version": SCHEMA_VERSION,
                "database": inventory,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "files": file_inventory(destination),
            }
            (destination / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            verify_bundle(destination)
            return manifest


def restore(database_url, bundle, workspace_root):
    bundle = _unlinked(bundle)
    manifest = verify_bundle(bundle)
    with workspace_lease(workspace_root) as workspace:
        if (
            workspace.exists()
            or workspace.is_relative_to(bundle)
            or bundle.is_relative_to(workspace)
        ):
            raise ValueError("Restore requires a separate, nonexistent workspace")
        with maintenance_connection(database_url, operation="restore"):
            with psycopg.connect(
                to_checkpoint_url(database_url), autocommit=True
            ) as connection:
                # A database with no tables can still contain views, functions,
                # types, or other user objects. Refuse them before --clean is used.
                occupied = connection.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_namespace "
                    "WHERE nspname NOT IN ('public','information_schema','rc_maintenance') "
                    "AND nspname !~ '^pg_') OR EXISTS ("
                    "SELECT 1 FROM pg_depend d JOIN pg_namespace n "
                    "ON d.refobjid=n.oid WHERE d.refclassid='pg_namespace'::regclass "
                    "AND n.nspname='public')"
                ).fetchone()[0]
                if occupied:
                    raise ValueError("Restore requires an empty dedicated database")
            # There is no transaction spanning files and PostgreSQL. Keep incomplete
            # targets on failure, and never declare success until both inventories match.
            workspace.mkdir(parents=True, exist_ok=False)
            _pg_tool(
                [
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-acl",
                    str(bundle / "database.dump"),
                ],
                database_url,
            )
            shutil.copytree(bundle / "workspace", workspace, dirs_exist_ok=True)
            with psycopg.connect(to_checkpoint_url(database_url)) as connection:
                _validate_links(connection)
                if database_inventory(connection) != manifest["database"]:
                    raise ValueError(
                        "Restored database inventory mismatch; target retained for diagnosis"
                    )
            if file_inventory(workspace) != file_inventory(bundle / "workspace"):
                raise ValueError(
                    "Restored workspace inventory mismatch; target retained for diagnosis"
                )
            return manifest
