"""The local fixed actor is not remote authentication, including read routes."""

import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys

import httpx
import pytest

from research_copilot.web.app import create_app


@pytest.mark.parametrize("path", ["/api/release", "/api/live", "/openapi.json"])
@pytest.mark.parametrize(
    "headers",
    [
        {"host": "attacker.invalid"},
        {"host": "127.0.0.1.attacker.invalid"},
        {"origin": "https://attacker.invalid"},
        {"origin": "null"},
        {"sec-fetch-site": "cross-site"},
        [("host", "localhost"), ("host", "attacker.invalid")],
        [("origin", "http://localhost"), ("origin", "https://attacker.invalid")],
    ],
)
async def test_untrusted_reads_are_rejected_before_routes(path, headers):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app()), base_url="http://localhost"
    ) as client:
        response = await client.get(path, headers=headers)
    assert response.status_code == 403
    assert "release_id" not in response.text


@pytest.mark.parametrize(
    "host", ["localhost", "127.0.0.1:8000", "[::1]:8000", "127.0.0.1:5174"]
)
async def test_local_reads_and_vite_preserved_host_remain_usable(host):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app()), base_url=f"http://{host}"
    ) as client:
        response = await client.get(
            "/api/release",
            headers={"origin": f"http://{host}", "sec-fetch-site": "same-origin"},
        )
        assert response.status_code == 200
        assert (await client.get("/api/live")).status_code == 200


async def test_actual_cli_transport_ignores_forwarded_headers(tmp_path):
    # Replace only dependency acquisition; exercise the real CLI/Uvicorn and
    # request guard through a TCP socket. No PG, model, or paid source is opened.
    child = """
import os
from types import SimpleNamespace
from fastapi import Request
from research_copilot import cli
from research_copilot.llm import config
from research_copilot.web import app as web
cli._settings_or_exit = lambda: SimpleNamespace(deepseek_api_key=None)
config.load_models_config = lambda path: SimpleNamespace(providers={})
def build(**kwargs):
    app = web.create_app()
    @app.get('/fixture-transport')
    async def probe(request: Request):
        return {'pid': os.getpid(), 'scheme': request.url.scheme, 'peer': request.client.host}
    return app
web.build_server_app = build
cli.app()
"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=os.pathsep.join(sys.path),
        FORWARDED_ALLOW_IPS="*",
        WEB_CONCURRENCY="8",
    )
    with (tmp_path / "server.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys._base_executable,
                "-c",
                child,
                "serve",
                "--models",
                "fixture.toml",
                "--api-only",
                "--port",
                str(port),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2
            ) as client:
                async with asyncio.timeout(30):
                    while True:
                        assert process.poll() is None, (
                            tmp_path / "server.log"
                        ).read_text(errors="replace")
                        try:
                            response = await client.get("/fixture-transport")
                            break
                        except (httpx.ConnectError, httpx.ConnectTimeout):
                            await asyncio.sleep(0.05)
                assert response.json()["pid"] == process.pid
                forwarded = {
                    "x-forwarded-proto": "https",
                    "x-forwarded-for": "203.0.113.1",
                }
                response = await client.get("/fixture-transport", headers=forwarded)
                assert response.json() == {
                    "pid": process.pid,
                    "scheme": "http",
                    "peer": "127.0.0.1",
                }
                response = await client.get(
                    "/api/release",
                    headers={**forwarded, "origin": f"https://127.0.0.1:{port}"},
                )
                assert response.status_code == 403
                assert (
                    await client.get("/api/release", headers={"host": "rebind.invalid"})
                ).status_code == 403
                assert (await client.get("/api/release")).status_code == 200
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                await asyncio.to_thread(process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=10)
