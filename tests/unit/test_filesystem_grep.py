"""Ripgrep output decoding must not depend on the Windows process locale."""

from deepagents.backends import FilesystemBackend

from research_copilot.workspace.filesystem import ReportFilesystemBackend


def test_grep_falls_back_to_utf8_python_search_when_ripgrep_output_is_undecodable(
    tmp_path, monkeypatch
):
    (tmp_path / "report.json").write_text(
        '{"revenue": "营业收入 123456 元，同比增长 5%"}', encoding="utf-8"
    )
    backend = ReportFilesystemBackend(root_dir=tmp_path)

    def undecodable(*args, **kwargs):
        raise UnicodeDecodeError("gbk", b"\x8c", 531, 532, "illegal multibyte sequence")

    monkeypatch.setattr(FilesystemBackend, "_ripgrep_search", undecodable)

    result = backend.grep("营业收入", "/report.json", max_count=8)

    assert result.matches
    assert "123456" in result.matches[0]["text"]
