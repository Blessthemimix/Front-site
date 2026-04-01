"""Simple in-memory async rate limiter."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class RateLimiter:
    """Fixed-window-esque limiter backed by per-key timestamp deques."""

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        """Return True if request should be allowed."""
        now = time.time()
        min_allowed = now - 60
        async with self._lock:
            q = self._events[key]
            while q and q[0] < min_allowed:
                q.popleft()
            if len(q) >= self._max:
                return False
            q.append(now)
            return True
