from __future__ import annotations

import asyncio
from collections import OrderedDict

from agent_runtime.feishu.events import FeishuMessageEvent, queue_key_for_event


class PerThreadQueue:
    def __init__(self, max_items: int = 1000) -> None:
        self.max_items = max(1, max_items)
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()

    def clear(self) -> None:
        self._locks.clear()

    def lock_for_event(self, event: FeishuMessageEvent) -> asyncio.Lock:
        key = queue_key_for_event(event)
        lock = self._locks.get(key)
        if lock is not None:
            self._locks.move_to_end(key)
            return lock
        self._evict_if_needed()
        lock = asyncio.Lock()
        self._locks[key] = lock
        return lock

    def _evict_if_needed(self) -> None:
        while len(self._locks) >= self.max_items:
            for existing_key, existing_lock in list(self._locks.items()):
                if not existing_lock.locked():
                    self._locks.pop(existing_key, None)
                    return
            self._locks.popitem(last=False)
