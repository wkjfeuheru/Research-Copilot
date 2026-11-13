import pytest

from research_copilot.sandbox.runner import DockerRunner, ExecutionUnknown


@pytest.mark.parametrize("remaining", ["container", "volume", "daemon", None])
async def test_cleanup_requires_successful_empty_container_and_volume_queries(
    remaining,
):
    runner = DockerRunner()
    calls = []

    async def command(*args, **kwargs):
        calls.append(args)
        if remaining == "daemon":
            return 1, b"", b"Cannot reach daemon", False
        kind = "volume" if args[0] == "volume" else "container"
        return 0, b"remaining" if remaining == kind else b"", b"", False

    runner._command = command
    if remaining:
        with pytest.raises(ExecutionUnknown):
            await runner.confirm_cleanup("exec-" + "a" * 32)
    else:
        await runner.confirm_cleanup("exec-" + "a" * 32)
        assert len(calls) == 2
