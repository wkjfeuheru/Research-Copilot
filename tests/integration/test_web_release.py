"""Release pairing uses actual app schema and bytes; no DB/model construction."""

import json
from pathlib import Path

import httpx
import pytest

from research_copilot.web.app import create_app


def site(tmp_path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text(
        '<script src="/assets/app.js"></script>', encoding="utf-8"
    )
    (root / "assets").mkdir()
    (root / "assets/app.js").write_text("window.fixture='original';", encoding="utf-8")
    return root


def sealed(tmp_path):
    from research_copilot.runtime.web_release import seal_web

    root = site(tmp_path)
    manifest = seal_web(root, frontend_source=Path("frontend"))
    return root, manifest


def test_unpaired_static_directory_is_rejected_before_host_start(tmp_path):
    with pytest.raises(ValueError, match="release"):
        create_app(static_dir=site(tmp_path))


@pytest.mark.parametrize("change", ["modify", "extra", "missing"])
def test_static_byte_inventory_must_match_release(tmp_path, change):
    root, _ = sealed(tmp_path)
    if change == "modify":
        (root / "assets/app.js").write_text("different", encoding="utf-8")
    elif change == "extra":
        (root / "old.js").write_text("stale", encoding="utf-8")
    else:
        (root / "assets/app.js").unlink()
    with pytest.raises(ValueError, match="release"):
        create_app(static_dir=root)


@pytest.mark.parametrize("change", ["runtime", "backend", "api"])
def test_incompatible_backend_or_runtime_is_rejected(tmp_path, monkeypatch, change):
    from research_copilot.runtime import web_release

    root, _ = sealed(tmp_path)
    if change == "runtime":
        original = web_release.runtime_identity
        monkeypatch.setattr(
            web_release, "runtime_identity", lambda: {**original(), "python": "changed"}
        )
    elif change == "backend":
        monkeypatch.setattr(web_release, "backend_digest", lambda: "f" * 64)
    else:
        monkeypatch.setattr(web_release, "API_VERSION", "incompatible")
    with pytest.raises(ValueError, match="release"):
        create_app(static_dir=root)


async def test_paired_static_snapshot_and_diagnostics_survive_directory_change(
    tmp_path,
):
    root, manifest = sealed(tmp_path)
    app = create_app(static_dir=root)
    (root / "assets/app.js").write_text("mutated after startup", encoding="utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        response = await client.get("/assets/app.js")
        assert (
            response.status_code == 200
            and response.text == "window.fixture='original';"
        )
        assert response.headers["x-content-type-options"] == "nosniff"
        assert (await client.get("/reports/fixture")).status_code == 200
        assert (await client.get("/assets/missing.js")).status_code == 404
        assert (await client.get("/api/does-not-exist")).status_code == 404
        assert (await client.get("/research-copilot-release.json")).status_code == 404
        release = (await client.get("/api/release")).json()
        assert release["release_id"] == manifest["release_id"]
        assert release["static_verified"] is True
        assert "packages" not in json.dumps(release)


async def test_api_only_mode_is_explicitly_unpaired():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app()), base_url="http://localhost"
    ) as client:
        release = await client.get("/api/release")
        assert release.status_code == 200
        assert release.json()["release_id"] is None
        assert release.json()["static_verified"] is False


def test_sealing_refuses_to_overwrite_existing_release(tmp_path):
    from research_copilot.runtime.web_release import seal_web

    root, manifest = sealed(tmp_path)
    with pytest.raises(ValueError, match="already"):
        seal_web(root, frontend_source=Path("frontend"))
    assert (
        json.loads((root / "research-copilot-release.json").read_text("utf-8"))
        == manifest
    )


def test_actual_openapi_change_requires_a_new_release(tmp_path):
    from research_copilot.runtime.web_release import load_web_release

    root, _ = sealed(tmp_path)
    app = create_app()

    @app.get("/api/new-contract")
    async def added():
        return {"new": True}

    with pytest.raises(ValueError, match="API schema mismatch"):
        load_web_release(app, root)


def test_hardlinked_static_file_is_rejected(tmp_path):
    import os
    from research_copilot.runtime.web_release import seal_web

    root = site(tmp_path)
    os.link(root / "assets/app.js", tmp_path / "alias.js")
    with pytest.raises(ValueError, match="unaliased"):
        seal_web(root, frontend_source=Path("frontend"))


def test_frontend_declared_api_mismatch_cannot_be_sealed(tmp_path):
    from research_copilot.runtime.web_release import seal_web

    source = tmp_path / "frontend"
    source.mkdir()
    (source / "package.json").write_text(
        json.dumps({"version": "0.1.0", "researchCopilotApiVersion": "other"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="frontend API"):
        seal_web(site(tmp_path), frontend_source=source)
