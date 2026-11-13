"""Disk routing with link rejection, including Windows junctions.

Deep Agents 0.7.32 resolves paths before accessing them. Override that pinned
resolution seam to reject aliases before resolution; public search methods also
inspect descendants because a recursive search can otherwise cross a link.
https://docs.langchain.com/oss/python/deepagents/backends
"""

from pathlib import Path, PurePosixPath

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import GrepResult
from deepagents.backends.utils import compile_grep_include_glob


def reject_links(path: Path) -> Path:
    path = path.absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ValueError("Linked filesystem paths are not authorized")
    return path


def tree_files(root: Path):
    """Enumerate without ever descending through a linked directory."""
    reject_links(root)
    if not root.exists():
        return
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            reject_links(child)
            if child.is_dir():
                pending.append(child)
            else:
                yield child


class ReportFilesystemBackend(FilesystemBackend):
    def __init__(self, *, root_dir: Path, read_only: bool = False):
        reject_links(root_dir)
        super().__init__(root_dir=root_dir.resolve(), virtual_mode=True)
        self.read_only = read_only

    def _resolve_path(self, key: str) -> Path:
        if (
            not key.startswith("/")
            or any(c in key for c in ("\\", ":", "~"))
            or ".." in PurePosixPath(key).parts
        ):
            raise ValueError("Invalid virtual filesystem path")
        reject_links(self.cwd / key.lstrip("/"))
        return super()._resolve_path(key)

    def _check_tree(self, path):
        root = self._resolve_path(path or "/")
        if root.is_dir():
            for _ in tree_files(root):
                pass

    def ls(self, path):
        self._check_tree(path)
        return super().ls(path)

    def glob(self, pattern, path=None):
        self._check_tree(path)
        return super().glob(pattern, path)

    def grep(self, pattern, path=None, glob=None, *, max_count=None, context_lines=0):
        self._check_tree(path)
        target = self._resolve_path(path or "/")
        if target.is_file():
            # The pinned backend's Python fallback searches the parent directory
            # for file paths. Keep a single-file request exact on every platform.
            return self._grep_file(target, pattern, glob, max_count, context_lines)
        return super().grep(
            pattern, path, glob, max_count=max_count, context_lines=context_lines
        )

    def _grep_file(self, target, pattern, glob, max_count, context_lines):
        if context_lines < 0:
            raise ValueError("context_lines must be non-negative")
        if glob:
            try:
                included = compile_grep_include_glob(glob)(target.name)
            except ValueError:
                return GrepResult(error="Invalid include glob", matches=[])
            if not included:
                return GrepResult(matches=[])
        try:
            if target.stat().st_size > self.max_file_size_bytes:
                return GrepResult(error="File exceeds search size limit", matches=[])
            lines = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return GrepResult(
                error="Unable to read requested file as UTF-8", matches=[]
            )
        indices = [i for i, line in enumerate(lines) if pattern in line]
        selected = indices if max_count is None else indices[: max(0, max_count)]
        virtual_path = "/" + target.relative_to(self.cwd).as_posix()
        matches = []
        for i in selected:
            match = {"path": virtual_path, "line": i + 1, "text": lines[i]}
            if context_lines:
                for key, positions in (
                    ("context_before", range(max(0, i - context_lines), i)),
                    (
                        "context_after",
                        range(i + 1, min(len(lines), i + context_lines + 1)),
                    ),
                ):
                    match[key] = [
                        {"line": j + 1, "text": lines[j]}
                        for j in positions
                        if j not in indices
                    ]
            matches.append(match)
        return GrepResult(matches=matches, truncated=len(indices) > len(selected))

    def _ripgrep_search(self, pattern, base_full, include_glob, max_count=None):
        # deepagents 0.7.32 decodes ripgrep's UTF-8 stdout with the process
        # locale codec; on Chinese Windows (cp936) Chinese evidence aborts the
        # tool with UnicodeDecodeError. Returning None selects the base UTF-8
        # Python search instead.
        try:
            return super()._ripgrep_search(pattern, base_full, include_glob, max_count)
        except UnicodeDecodeError:
            return None, False

    def _check_write(self):
        if self.read_only:
            raise PermissionError("Rules and skills are read-only resources")

    def write(self, file_path, content):
        self._check_write()
        return super().write(file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        self._check_write()
        return super().edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path):
        self._check_write()
        return super().delete(file_path)

    def upload_files(self, files):
        self._check_write()
        return super().upload_files(files)


class ResourceFilesystemBackend(ReportFilesystemBackend):
    """Install-time bytecode is never an agent-visible or executable skill asset."""

    def _resolve_path(self, key):
        key = key.replace(
            "/analysist/financial_modeling/", "/analysist/financial-modeling/"
        )
        if key.endswith("/analysist/financial_modeling"):
            key = key[: -len("financial_modeling")] + "financial-modeling"
        if "__pycache__" in {part.casefold() for part in PurePosixPath(key).parts}:
            raise PermissionError(
                "Interpreter caches are not published skill resources"
            )
        return super()._resolve_path(key)

    def ls(self, path):
        result = super().ls(path)
        if result.entries is not None:
            result.entries = [
                entry
                for entry in result.entries
                if "__pycache__" not in PurePosixPath(entry["path"]).parts
            ]
        return result

    def glob(self, pattern, path=None):
        result = super().glob(pattern, path)
        if result.matches is not None:
            result.matches = [
                entry
                for entry in result.matches
                if "__pycache__" not in PurePosixPath(entry["path"]).parts
            ]
        return result

    def grep(self, pattern, path=None, glob=None, *, max_count=None, context_lines=0):
        result = super().grep(
            pattern, path, glob, max_count=max_count, context_lines=context_lines
        )
        if result.matches is not None:
            result.matches = [
                entry
                for entry in result.matches
                if "__pycache__" not in PurePosixPath(entry["path"]).parts
            ]
        return result
