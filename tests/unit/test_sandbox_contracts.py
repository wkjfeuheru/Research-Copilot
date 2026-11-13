"""Reject hostile output and freeze deterministic execution candidates."""

import importlib.util
import io
import tarfile
import pytest


def sandbox_module(name):
    assert importlib.util.find_spec("research_copilot.sandbox"), (
        "Sandbox package is missing"
    )
    return __import__("research_copilot.sandbox." + name, fromlist=[name])


def test_execution_limits_cannot_disable_timeout():
    cfg = sandbox_module("config").SandboxConfig()
    assert cfg.effective_timeout(0) == 120
    assert cfg.effective_timeout(10000) == 120
    assert cfg.effective_timeout(7) == 7


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/etc/passwd",
        "x/../../out",
        "a\\b",
        "C:/foo",
        "NUL.txt",
        "a/COM1",
        "alias.",
        "alias ",
    ],
)
def test_manifest_rejects_escaping_paths(path):
    with pytest.raises(ValueError):
        sandbox_module("contracts").relative_path(path)


def test_output_archive_rejects_symlink_and_size(tmp_path):
    module = sandbox_module("runner")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        link = tarfile.TarInfo("outputs/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
    with pytest.raises(ValueError, match="link|regular"):
        module.import_archive(
            stream.getvalue(), tmp_path, sandbox_module("config").SandboxConfig()
        )


@pytest.mark.parametrize(
    "option",
    [
        {"series": [{"type": "custom", "renderItem": "function(){}"}]},
        {
            "series": [{"type": "line", "data": [1]}],
            "tooltip": {"formatter": "<img src=x onerror=alert(1)>"},
        },
        {
            "series": [{"type": "line", "data": [1]}],
            "graphic": {"type": "image", "style": {"image": "https://evil"}},
        },
    ],
)
def test_echarts_policy_rejects_code_external_resources(option):
    with pytest.raises(ValueError):
        sandbox_module("contracts").validate_option(option)


@pytest.mark.parametrize(
    "option",
    [
        {"series": "blank"},
        {"series": [{}]},
        {"series": [{"type": "category", "data": [1]}]},
    ],
)
def test_echarts_requires_actual_supported_series(option):
    with pytest.raises(ValueError):
        sandbox_module("contracts").validate_option(option)


def test_result_rejects_nonfinite_and_duplicate_metrics():
    m = sandbox_module("contracts")
    for metrics in [
        [{"key": "x", "value": "NaN", "unit": "ratio", "input_keys": ["a"]}],
        [{"key": "x", "value": "1", "unit": "ratio", "input_keys": ["a"]}] * 2,
    ]:
        with pytest.raises(ValueError):
            m.ResultManifest.model_validate(
                {
                    "schema_version": 1,
                    "metrics": metrics,
                    "charts": [],
                    "input_refs": [],
                    "assumptions": {},
                    "checks": [],
                    "limitations": [],
                }
            )


def test_orphan_probe_is_readonly_and_detects_current_process():
    import os

    runner = sandbox_module("runner")
    assert hasattr(runner, "process_alive"), (
        "Need a Windows-safe readonly process probe"
    )
    assert runner.process_alive(os.getpid())
    assert not runner.process_alive(2147483647)


def test_metering_preserves_explicit_model_image_capability():
    from tests.unit.test_orchestration import ScriptModel
    from research_copilot.llm import MeteredChatModel
    from research_copilot.runtime.budget import TokenBudget

    model = ScriptModel(
        script=[],
        profile={
            "max_input_tokens": 100000,
            "max_output_tokens": 1000,
            "image_inputs": True,
        },
    )
    wrapped = MeteredChatModel.wrap(
        model,
        budget=TokenBudget(limit=1000000, budget_id="test"),
        max_input_tokens=100000,
        max_output_tokens=1000,
    )
    assert wrapped.profile.get("image_inputs") is True


def test_installed_bytecode_is_not_a_runtime_skill_asset(tmp_path):
    import shutil
    import py_compile
    from research_copilot.workspace import bundled_resources, validate_resources

    root = tmp_path / "resources"
    shutil.copytree(bundled_resources(), root)
    source = root / "skills/analysist/financial-modeling/scripts/growth.py"
    cache = py_compile.compile(str(source), doraise=True)
    assert cache
    validate_resources(root)
    from research_copilot.workspace import resource_files

    assert not any(path.suffix == ".pyc" for path in resource_files(root))
    from research_copilot.workspace import build_backend

    backend = build_backend(tmp_path / "report", root)
    script_root = "/skills/analysist/financial-modeling/scripts/"
    assert not any(
        "__pycache__" in item["path"] for item in backend.ls(script_root).entries
    )
    assert not any(
        "__pycache__" in item["path"]
        for item in backend.glob("**/*", script_root).matches
    )
    assert not any(
        "__pycache__" in item["path"]
        for item in backend.grep("Decimal", script_root).matches
    )
    denied = backend.read(
        script_root + "__pycache__/" + __import__("pathlib").Path(cache).name
    )
    assert denied.error and "caches" in denied.error and not denied.file_data
    alias = backend.read(
        script_root + "__PYCACHE__/" + __import__("pathlib").Path(cache).name
    )
    assert alias.error and not alias.file_data
