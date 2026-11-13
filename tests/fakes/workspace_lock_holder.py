"""Independent native lock holder; never starts services or external work."""

import os
from pathlib import Path
import sys
import time

from filelock import FileLock, Timeout
from research_copilot.runtime.workspace_lock import workspace_lease

root, signal, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
lock = (
    FileLock(str(root) + ".host.lock", timeout=0)
    if mode == "legacy"
    else workspace_lease(root, shared=mode == "shared")
)
try:
    with lock:
        signal.write_text(str(os.getpid()), encoding="utf-8")
        if "probe" not in sys.argv[4:]:
            while not signal.with_suffix(".stop").exists():
                time.sleep(0.02)
except Timeout:
    signal.write_text("blocked", encoding="utf-8")
