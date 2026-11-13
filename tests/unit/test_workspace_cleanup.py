import pytest

from research_copilot.web.app import _remove_workspace
from tests.unit.test_workspace_policy import link_directory


@pytest.mark.parametrize("location", ["report", "child", "root"])
def test_cleanup_never_follows_a_report_link_to_another_report(tmp_path, location):
    root = tmp_path / "workspace"
    peer = root / "peer-report"
    peer.mkdir(parents=True)
    (peer / "evidence.txt").write_text("Retain another report", encoding="utf-8")
    selected = "delete-report"
    if location == "child":
        (root / selected).mkdir()
        alias = root / selected / "alias"
        target = peer
    elif location == "root":
        alias, target = tmp_path / "alias-workspace", root
        selected = "peer-report"
    else:
        alias, target = root / selected, peer
    link_directory(alias, target)
    try:
        removed, errors = _remove_workspace(
            alias if location == "root" else root, selected
        )
        assert removed is False and errors
        assert (peer / "evidence.txt").read_text("utf-8") == "Retain another report"
    finally:
        alias.rmdir() if alias.is_junction() else alias.unlink()
