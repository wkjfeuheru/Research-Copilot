from types import SimpleNamespace

import pytest

from research_copilot.runtime import web_release


def test_runtime_closure_includes_requested_extras_and_excludes_dev(monkeypatch):
    graph = {
        "research-copilot": (
            "0.1.0",
            [
                "core[b]>=1",
                "bridge",
                'dev-only; extra == "dev"',
                'unavailable; sys_platform == "never"',
            ],
        ),
        "bridge": ("1.0", ["core[a]>=1"]),
        "core": ("1.2", ['feature-a; extra == "a"', 'feature-b; extra == "b"']),
        "feature-a": ("2.0", []),
        "feature-b": ("3.0", []),
    }
    monkeypatch.setattr(
        web_release.metadata,
        "distribution",
        lambda name: SimpleNamespace(version=graph[name][0], requires=graph[name][1]),
    )
    monkeypatch.setattr(web_release.metadata, "version", lambda name: graph[name][0])
    assert web_release.runtime_identity()["packages"] == {
        name: value[0] for name, value in graph.items()
    }


def test_unsatisfied_installed_dependency_cannot_be_sealed(monkeypatch):
    monkeypatch.setattr(
        web_release.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="0.1.0", requires=["core>=2"]),
    )
    monkeypatch.setattr(web_release.metadata, "version", lambda name: "1.0")
    with pytest.raises(ValueError, match="dependency is incompatible"):
        web_release.runtime_identity()
