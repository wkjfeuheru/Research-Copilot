"""默认测试环境：隔离 RC_ 环境变量，保证本地 .env 与真实密钥不参与断言。"""

from __future__ import annotations

import asyncio
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_rc_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """移除进程中的 RC_* 变量；需要时由用例自行 monkeypatch.setenv。

    ``RC_TEST_DATABASE_URL`` 例外：它是测试专用连接串，不属于应用配置，保留后
    数据库集成用例才能在 fixture 中读到它（仍由 ``-m db`` 显式控制是否连接）。
    """

    for name in list(os.environ):
        if name.upper().startswith("RC_") and name.upper() != "RC_TEST_DATABASE_URL":
            monkeypatch.delenv(name, raising=False)

    from research_copilot.runtime.host_settings import HostSettings

    monkeypatch.setitem(HostSettings.model_config, "env_file", None)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """``db`` / ``live`` 用例默认跳过，显式 ``-m db`` 或 ``-m live`` 时才运行。"""

    markexpr = config.getoption("markexpr", default="") or ""
    if "load" not in markexpr:
        for item in items:
            if item.get_closest_marker("load"):
                item.add_marker(
                    pytest.mark.skip(reason="负载实验需显式 -m 'db and load'")
                )
    if "docker" not in markexpr:
        docker_skip = pytest.mark.skip(
            reason="真实沙盒需预构建镜像，显式 pytest -m docker 执行"
        )
        for item in items:
            if item.get_closest_marker("docker"):
                item.add_marker(docker_skip)
    if "db" in markexpr or "live" in markexpr:
        return

    skip = pytest.mark.skip(
        reason="需要显式选择：pytest -m db 或 pytest -m live（默认测试不访问数据库或真实模型）"
    )
    for item in items:
        if item.get_closest_marker("db") or item.get_closest_marker("live"):
            item.add_marker(skip)


def pytest_asyncio_loop_factories(config: pytest.Config, item: pytest.Item):
    """让异步测试与生产运行时使用同一类事件循环。

    Windows 上生产运行时使用 ``SelectorEventLoop``（psycopg 异步模式的要求），
    测试也必须如此，否则数据库用例会在 Proactor 循环上直接失败。
    """

    from research_copilot.runtime.loop import main_loop_factory

    factory = main_loop_factory("auto")
    if factory is None:  # pragma: no cover - 非 Windows 平台
        return {"default": asyncio.new_event_loop}
    return {"selector": factory}
