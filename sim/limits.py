"""One process-wide limit on concurrently running what-if simulations, shared by the API job registry and the agent.

`AMARH_SCN_MAX_SIM_JOBS` (default 2) slots. `acquire_sim_slot(timeout)` -> bool; `release_sim_slot()` after it.
"""
from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_sem: threading.BoundedSemaphore | None = None
_size = 0


def max_slots() -> int:
    return max(1, int(os.environ.get("AMARH_SCN_MAX_SIM_JOBS", "2")))


def _get() -> threading.BoundedSemaphore:
    global _sem, _size
    n = max_slots()
    with _lock:
        if _sem is None or _size != n:      # the env limit changed (tests): start a fresh pool
            _sem, _size = threading.BoundedSemaphore(n), n
        return _sem


def acquire_sim_slot(timeout: float = 0.0) -> bool:
    sem = _get()
    return sem.acquire(timeout=timeout) if timeout > 0 else sem.acquire(blocking=False)


def release_sim_slot() -> None:
    try:
        _get().release()
    except ValueError:      # released more than acquired, or the pool was resized: ignore
        pass
