"""Process-local measurements for an explicitly enabled, offline load fixture."""

import os
from pathlib import Path
import sys
import time


def memory_bytes():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "peak_working",
                    "working",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                )
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        query = ctypes.windll.psapi.GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        query.restype = wintypes.BOOL
        if not query(wintypes.HANDLE(-1), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError()
        return counters.working, "working_set"
    if sys.platform.startswith("linux"):
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf(
            "SC_PAGE_SIZE"
        ), "rss"
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "peak_rss_bytes_macos"


def sample(backend):
    memory, kind = memory_bytes()
    pool = backend.store.engine.pool
    return {
        "time": time.time(),
        "pid": os.getpid(),
        "cpu_seconds": time.process_time(),
        "memory_bytes": memory,
        "memory_kind": kind,
        "sql_pool": {
            "size": pool.size(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
        },
        "checkpoint_pool": backend.session.agent_factory.checkpointer.conn.get_stats(),
    }
