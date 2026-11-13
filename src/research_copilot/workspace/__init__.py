"""Report filesystem routing and validation of packaged resources."""

import hashlib
from importlib.util import source_from_cache
import json
import re
from pathlib import Path

from deepagents.backends import CompositeBackend, StateBackend

from .filesystem import (
    ReportFilesystemBackend,
    ResourceFilesystemBackend,
    reject_links,
    tree_files,
)

REPORT_TYPES = ("company_report", "flash_comment", "industry_report", "strategy_report")


def safe_component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError("Invalid report/directory identifier")
    return value


def build_backend(
    report_root: Path,
    resources_root: Path,
    *,
    artifacts_root="/workspace",
    read_only=False,
):
    validate_resources(resources_root)
    reject_links(report_root)
    if not read_only:
        report_root.mkdir(parents=True, exist_ok=True)
        for name in ("supervisor", "tasks", "inputs", "evidence", "artifacts"):
            reject_links(report_root / name).mkdir(exist_ok=True)
    return CompositeBackend(
        default=StateBackend(),
        routes={
            "/workspace/": ReportFilesystemBackend(
                root_dir=report_root, read_only=read_only
            ),
            "/instructions/": ResourceFilesystemBackend(
                root_dir=resources_root / "instructions", read_only=True
            ),
            "/skills/": ResourceFilesystemBackend(
                root_dir=resources_root / "skills", read_only=True
            ),
        },
        # Summarization offloads evicted history here. A review subgraph has no
        # `files` state, so the default StateBackend would raise; the report
        # filesystem is durable and already authorized.
        artifacts_root=artifacts_root,
    )


def validate_resources(root: Path) -> None:
    root = reject_links(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    if manifest.get("resource_version") != 1:
        raise ValueError("Unsupported resource version")
    hashes = manifest["output_hashes"]

    def installed_bytecode(path):
        if path.parent.name != "__pycache__" or path.suffix != ".pyc":
            return False
        try:
            source = Path(source_from_cache(str(path)))
            return source.relative_to(root).as_posix() in hashes
        except ValueError:
            return False

    # pip/uv may compile package data .py files at install time. Derived caches are
    # never exposed as skills or copied to the execution snapshot.
    actual = {
        path.relative_to(root).as_posix()
        for path in tree_files(root)
        if not installed_bytecode(path)
    }
    if actual != set(hashes) | {"manifest.json"}:
        raise ValueError("Unexpected or unmanifested resource files")
    required = {
        "instructions/AGENTS.md",
        "skills/analysist/financial-modeling/SKILL.md",
        "skills/writer/reports/SKILL.md",
        *(f"skills/writer/reports/references/{kind}.md" for kind in REPORT_TYPES),
    }
    if not required <= hashes.keys():
        raise ValueError("Incomplete resource manifest")
    for relative, expected in hashes.items():
        if (
            not isinstance(relative, str)
            or "\\" in relative
            or ":" in relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
        ):
            raise ValueError("Invalid resource manifest path")
        path = root / relative
        if (
            not path.resolve().is_relative_to(root)
            or path.is_symlink()
            or path.is_junction()
        ):
            raise ValueError("Resource path escapes its package")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Resource hash mismatch: {relative}")
    for role, package in (("writer", "reports"), ("analysist", "financial-modeling")):
        if {path.name for path in (root / "skills" / role).iterdir()} != {package}:
            raise ValueError(f"Unexpected {role} skill package")


def bundled_resources() -> Path:
    """Validated packaged resources shipped with the Python package."""
    root = Path(__file__).parent.parent / "resources"
    validate_resources(root)
    return root


def resource_files(root: Path):
    """Only hash-verified source assets, excluding interpreter-generated caches."""
    validate_resources(root)
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    return [root / relative for relative in sorted(manifest["output_hashes"])]
