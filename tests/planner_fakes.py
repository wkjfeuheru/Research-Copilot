"""Planner 测试替身；不进入生产路径。"""

from collections.abc import Iterable

from research_copilot.llm import ModelRequest, ModelResponse


class FakeStructuredModelClient:
    """按顺序返回固定原始文本，或抛出预设异常。"""

    def __init__(self, responses: Iterable[str | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("测试替身收到超出预期的模型调用")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ModelResponse(content=response, model="fake-model")
