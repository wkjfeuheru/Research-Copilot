from types import SimpleNamespace

from typer.testing import CliRunner

from research_copilot import cli


def test_api_only_explicitly_ignores_existing_static_build(tmp_path, monkeypatch):
    import uvicorn
    from research_copilot.llm import config
    from research_copilot.web import app as web

    static = tmp_path / "frontend/dist"
    static.mkdir(parents=True)
    (static / "index.html").write_text("old unpaired build", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli, "_settings_or_exit", lambda: SimpleNamespace(deepseek_api_key=None)
    )
    monkeypatch.setattr(
        config, "load_models_config", lambda path: SimpleNamespace(providers={})
    )
    captured = []

    def build(**kwargs):
        captured.append(kwargs)
        return "fixture-app"

    monkeypatch.setattr(web, "build_server_app", build)
    server_options = []
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setenv("WEB_CONCURRENCY", "8")
    monkeypatch.setattr(
        uvicorn, "run", lambda *args, **kwargs: server_options.append(kwargs)
    )
    result = CliRunner().invoke(
        cli.app, ["serve", "--models", "fixture.toml", "--api-only"]
    )
    assert result.exit_code == 0, result.output
    assert captured[0]["static_dir"] is None
    assert server_options[0]["host"] == "127.0.0.1"
    assert server_options[0].get("proxy_headers") is False
    assert server_options[0].get("workers") == 1


def test_api_only_and_static_directory_are_mutually_exclusive():
    result = CliRunner().invoke(
        cli.app,
        ["serve", "--models", "fixture.toml", "--api-only", "--static-dir", "dist"],
    )
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_history_only_startup_needs_no_model_configuration(tmp_path, monkeypatch):
    import uvicorn
    from research_copilot.web import app as web

    monkeypatch.setattr(
        cli, "_settings_or_exit", lambda: SimpleNamespace(database_url="isolated")
    )
    captured = []
    monkeypatch.setattr(web, "build_server_app", lambda **kw: captured.append(kw))
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kw: None)
    result = CliRunner().invoke(cli.app, ["serve", "--history-only", "--api-only"])
    assert result.exit_code == 0, result.output
    assert captured[0]["history_only"] and captured[0]["models_path"] is None
