"""Tiny in-memory sliding-window rate limiter (one app instance, as the spec assumes)."""
from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self.hits) > 10000:  # keep memory bounded
            for k in [k for k, v in self.hits.items() if not v][:5000]:
                del self.hits[k]
        return True

    def reset(self) -> None:
        self.hits.clear()


patient_pages = RateLimiter(limit=60, window_s=60)      # per IP
inbound_sms = RateLimiter(limit=5, window_s=600)        # per sender phone
logins = RateLimiter(limit=10, window_s=300)            # per IP
