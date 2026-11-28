"""A tiny in-process TTL cache for the public pages' expensive shared data (tournament
snapshots, field means).

Values must be plain data (dataclasses, dicts, numbers), never ORM objects: they outlive the
request's session. `clear()` is called by the middleware in `app/main.py` after any
successful non-GET request. Workers don't share it, so the TTL bounds how stale a write made
through another worker can look."""

import threading
import time
from collections.abc import Callable
from typing import TypeVar

from app.core.config import settings

T = TypeVar("T")

_lock = threading.Lock()
_store: dict[object, tuple[float, object]] = {}


def cached(key: object, build: Callable[[], T]) -> T:
    ttl = settings.public_cache_ttl_seconds
    if ttl <= 0:
        return build()
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]  # type: ignore[return-value]
    value = build()
    with _lock:
        _store[key] = (time.monotonic() + ttl, value)
    return value


def clear() -> None:
    with _lock:
        _store.clear()
