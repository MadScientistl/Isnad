"""In-process login throttling with lockout backoff (per process, not shared across workers; lost on restart).

Failures are counted per key (client IP, submitted user name) in a sliding window. Once a key reaches its
failure threshold it is locked for `lock_base_s * 2**(extra failures)` seconds (capped at `lock_max_s`). While
locked, even the correct password is refused (429), which is the point; the cost is that an attacker who
knows a user name can lock that user out for a while (accepted for v1; the lock decays). The submitted name
is used whether or not it exists, so throttling does not reveal which users exist."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict


class Throttle:
    def __init__(self, max_fails: int, window_s: float, lock_base_s: float, lock_max_s: float, max_keys: int = 10000):
        self.max_fails, self.window, self.base, self.cap, self.max_keys = max_fails, window_s, lock_base_s, lock_max_s, max_keys
        self._d: OrderedDict[str, list] = OrderedDict()   # key -> [fail timestamps, locked_until]
        self._lock = threading.Lock()

    def retry_after(self, key: str, now: float | None = None) -> int:
        """Seconds until the key may try again (0 = allowed)."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            e = self._d.get(key)
            return max(0, int(e[1] - now) + 1) if e and e[1] > now else 0

    def fail(self, key: str, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        with self._lock:
            e = self._d.setdefault(key, [[], 0.0])
            self._d.move_to_end(key)
            e[0] = [t for t in e[0] if now - t < self.window] + [now]
            if len(e[0]) >= self.max_fails:
                extra = min(len(e[0]) - self.max_fails, 20)
                e[1] = now + min(self.cap, self.base * (2 ** extra))
            while len(self._d) > self.max_keys:
                self._d.popitem(last=False)

    def success(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)
