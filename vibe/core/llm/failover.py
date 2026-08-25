from __future__ import annotations

import asyncio


class ModelFailoverState:
    def __init__(self) -> None:
        self._active_keys: set[str] = set()
        self._lock = asyncio.Lock()

    def active(self, key: str) -> bool:
        return key in self._active_keys

    async def activate(self, key: str) -> bool:
        async with self._lock:
            if key in self._active_keys:
                return False
            self._active_keys.add(key)
            return True

    def reset(self) -> None:
        self._active_keys.clear()
