from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from research_copilot import host
from research_copilot.runtime.host_settings import HostSettings
from research_copilot.runtime.storage import SqlSessionStore


def settings(**changes):
    return HostSettings(
        _env_file=None,
        database_url="postgresql://operator:secret@localhost/isolated",
        **changes,
    )


def options(tmp_path):
    return dict(
        models_path=Path("config/models.example.toml"),
        workspace_root=tmp_path / "workspace",
        policy_version="test-policy",
        mode="test",
    )


def test_configuration_digest_tracks_semantics_and_excludes_credentials(
    tmp_path, monkeypatch
):
    kwargs = options(tmp_path)
    kwargs.pop("workspace_root")
    first = host.prepare_host_configuration(settings(), **kwargs)[3]
    monkeypatch.setenv("RC_DEEPSEEK_API_KEY", "rotated-private-key")
    changed_credentials = settings(deepseek_api_key="another-private-key")
    changed_credentials.database_url = "postgresql://rotated:rotated@localhost/isolated"
    assert host.prepare_host_configuration(changed_credentials, **kwargs)[3] == first
    kwargs["policy_version"] = "changed-policy"
    assert host.prepare_host_configuration(settings(), **kwargs)[3] != first


def test_financekb_host_binding_includes_origin_and_pinned_contract(
    tmp_path, monkeypatch
):
    kwargs = options(tmp_path)
    kwargs.pop("workspace_root")

    def bound_fingerprint(url, token):
        return host.prepare_host_configuration(
            settings(financekb_base_url=url, financekb_api_token=token), **kwargs
        )[3]

    origin = "https://financekb.example.test"
    pinned = bound_fingerprint(origin, "first-token")
    assert pinned != host.prepare_host_configuration(settings(), **kwargs)[3]
    assert pinned == bound_fingerprint(origin, "rotated-token")
    assert pinned != bound_fingerprint("https://other.example.test", "first-token")
    monkeypatch.setitem(host.APPROVED_SCHEMAS["macro"], "get_macro_indicator", "a" * 64)
    assert pinned != bound_fingerprint(origin, "first-token")


async def test_unbound_host_refuses_before_model_clients_and_explicit_binding_has_no_models(
    tmp_path, monkeypatch
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'host.db'}")
    store = SqlSessionStore(engine)
    await store.setup()

    @asynccontextmanager
    async def database(*args, **kwargs):
        yield store, None

    @asynccontextmanager
    async def forbidden_models(*args, **kwargs):
        raise AssertionError("Model clients must not open before binding validation")
        yield

    monkeypatch.setattr(host, "postgres_runtime", database)
    monkeypatch.setattr(host, "open_models", forbidden_models)
    try:
        with pytest.raises(ValueError, match="unbound"):
            async with host.open_session(settings(), **options(tmp_path)):
                pytest.fail("Unbound host started")
        assert not options(tmp_path)["workspace_root"].exists()
        bound = await host.bind_host_configuration(
            settings(),
            **options(tmp_path),
            actor_id="operator",
            note="Reviewed test setup",
        )
        assert bound["revision"] == 1
        # Binding passes; only now can the client-construction sentinel be reached.
        with pytest.raises(AssertionError, match="Model clients"):
            async with host.open_session(settings(), **options(tmp_path)):
                pytest.fail("Unexpected session")
    finally:
        await engine.dispose()


def test_cli_bind_host_forwards_explicit_options(tmp_path, monkeypatch):
    from research_copilot import cli

    captured = {}

    async def bind(config, **kwargs):
        captured.update(kwargs)
        return {"revision": 2, "workspace_id": "a" * 32, "config_digest": "b" * 64}

    monkeypatch.setattr(host, "bind_host_configuration", bind)
    monkeypatch.setattr(cli, "_settings_or_exit", settings)
    result = CliRunner().invoke(
        cli.app,
        [
            "bind-host",
            "--models",
            "config/models.example.toml",
            "--workspace",
            str(tmp_path),
            "--actor-id",
            "operator",
            "--note",
            "Stopped writers and reviewed configuration",
            "--replace",
            "--mode",
            "test",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["replace"] is True and captured["mode"] == "test"
    assert captured["workspace_root"] == tmp_path
    assert '"revision": 2' in result.output


@pytest.mark.parametrize(
    "changed", [{"concurrency": 1}, {"image": "different:1"}, {"queue_seconds": 10}]
)
def test_host_binding_tracks_sandbox_configuration(tmp_path, monkeypatch, changed):
    from research_copilot.sandbox.config import SandboxConfig

    kwargs = options(tmp_path)
    kwargs.pop("workspace_root")
    monkeypatch.setattr(SandboxConfig, "load", lambda: SandboxConfig())
    original = host.prepare_host_configuration(settings(), **kwargs)[3]
    monkeypatch.setattr(SandboxConfig, "load", lambda: SandboxConfig(**changed))
    assert host.prepare_host_configuration(settings(), **kwargs)[3] != original


async def test_host_passes_the_bound_sandbox_snapshot_to_its_factory(
    tmp_path, monkeypatch
):
    from research_copilot.sandbox.config import SandboxConfig

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'host.db'}")
    store = SqlSessionStore(engine)
    await store.setup()
    captured = {}
    snapshot = SandboxConfig(concurrency=1)
    monkeypatch.setattr(SandboxConfig, "load", lambda: snapshot)

    @asynccontextmanager
    async def database(*args, **kwargs):
        yield store, None

    @asynccontextmanager
    async def models(*args, **kwargs):
        monkeypatch.setattr(SandboxConfig, "load", lambda: SandboxConfig(concurrency=2))
        yield {}

    def factory(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(host, "postgres_runtime", database)
    monkeypatch.setattr(host, "open_models", models)
    monkeypatch.setattr(host, "SupervisorFactory", factory)
    try:
        await host.bind_host_configuration(
            settings(), **options(tmp_path), actor_id="operator", note="Bound snapshot"
        )
        async with host.open_session(settings(), **options(tmp_path)):
            assert captured.get("sandbox_config") is snapshot
        # A later host sees the changed file and must fail binding validation.
        with pytest.raises(ValueError, match="binding"):
            async with host.open_session(settings(), **options(tmp_path)):
                pytest.fail("Changed sandbox configuration was silently adopted")
    finally:
        await engine.dispose()
