"""Bounded reuse of compiled readers, never checkpoint values or execution graphs."""

from collections import OrderedDict

from research_copilot.runtime.identity import checkpoint_thread_id


class StatusReader:
    __slots__ = ("_read", "_thread_id")

    def __init__(self, graph, report_id):
        self._read = graph.aget_state
        self._thread_id = checkpoint_thread_id(report_id)

    async def aget_state(self, config, *, subgraphs=False):
        # Status always means latest state for this authenticated report. Do not
        # accept another thread, historical checkpoint or checkpointer override.
        if config.get("configurable") != {"thread_id": self._thread_id}:
            raise ValueError("Status reader requires its own current report")
        return await self._read(config, subgraphs=subgraphs)


class StatusReaders:
    """Host-owned LRU. Lookup/build/put run without an intervening await."""

    def __init__(self, capacity=8):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Status reader capacity must be positive")
        self.capacity = capacity
        self._items = OrderedDict()

    def get(self, key):
        reader = self._items.get(key)
        if reader is not None:
            self._items.move_to_end(key)
        return reader

    def put(self, key, reader):
        # Keep only the latest identity for a report, even before reaching the
        # total limit. Running callers retain their own read-only reference.
        for previous in tuple(self._items):
            if previous[:2] == key[:2]:
                del self._items[previous]
        self._items[key] = reader
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)
