"""Native shared runtime/exclusive maintenance lock on a stable workspace sidecar.

All participants must use the same path and a filesystem with working native
locks. Network filesystems require deployment-specific verification; no soft
lease, TTL recovery, or process-local fallback is used here.
https://portalocker.readthedocs.io/en/latest/platforms.html
"""

from contextlib import contextmanager
import os
from pathlib import Path
import stat

from filelock import Timeout
import portalocker

from research_copilot.workspace.filesystem import reject_links


@contextmanager
def workspace_lease(root, *, shared=False):
    if type(shared) is not bool:
        raise ValueError("Workspace lock mode must be boolean")
    root = reject_links(Path(root)).resolve()
    root.parent.mkdir(parents=True, exist_ok=True)
    path = reject_links(Path(str(root) + ".host.lock"))
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        original = os.fstat(stream.fileno())
        if not stat.S_ISREG(original.st_mode) or original.st_nlink != 1:
            raise ValueError("Workspace lock must be a single-link regular file")
        mode = portalocker.LOCK_SH if shared else portalocker.LOCK_EX
        try:
            portalocker.lock(stream, mode | portalocker.LOCK_NB)
        except portalocker.AlreadyLocked:
            raise Timeout(str(path)) from None
        try:
            # Do not hold a detached inode or an alias to a replaced lock path.
            current = reject_links(path).stat()
            if not os.path.samestat(original, current) or current.st_nlink != 1:
                raise ValueError("Workspace lock path changed during acquisition")
            yield root
        finally:
            portalocker.unlock(stream)
    # Keep the sidecar: unlinking a lock creates separate ownership domains.
