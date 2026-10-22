"""测试夹具：隔离宿主环境中的 RC_ 配置。"""

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def clear_research_copilot_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """避免开发者本机的 ``RC_`` 环境变量影响配置与健康检查测试。"""

    for key in list(os.environ):
        if key.upper().startswith("RC_"):
            monkeypatch.delenv(key, raising=False)
    yield
