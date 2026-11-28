"""Brute-force brake for `POST /auth/login`.

Failed attempts are counted in a sliding window per client IP and per email; once either
reaches `login_max_failures`, further attempts get a 429 until the oldest failure in the
window ages out. A successful login clears that email's (and IP's) record.

In-process and per worker: with N gunicorn workers an attacker gets at most N times the
budget, which is still a hard cap at this app's scale and needs no shared store. The client
IP is `request.client.host`, which is only the real address when gunicorn's
`--forwarded-allow-ips` names the reverse proxy (see docs/deployment.md)."""

import threading
import time
from collections import deque

from app.core.config import settings

_lock = threading.Lock()
_failures: dict[str, deque[float]] = {}


def _keys(ip: str | None, email: str) -> list[str]:
    keys = [f"email:{email}"]
    if ip:
        keys.append(f"ip:{ip}")
    return keys


def _prune(entries: deque[float], now: float) -> None:
    cutoff = now - settings.login_window_seconds
    while entries and entries[0] <= cutoff:
        entries.popleft()


def retry_after(ip: str | None, email: str) -> int | None:
    """Seconds until another attempt is allowed, or None if it's allowed now."""
    if settings.login_max_failures <= 0:
        return None
    now = time.monotonic()
    wait = 0.0
    with _lock:
        for key in _keys(ip, email):
            entries = _failures.get(key)
            if not entries:
                continue
            _prune(entries, now)
            if len(entries) >= settings.login_max_failures:
                wait = max(wait, entries[0] + settings.login_window_seconds - now)
            if not entries:
                del _failures[key]
    return max(1, int(wait) + 1) if wait > 0 else None


def record_failure(ip: str | None, email: str) -> None:
    now = time.monotonic()
    with _lock:
        for key in _keys(ip, email):
            entries = _failures.setdefault(key, deque())
            _prune(entries, now)
            entries.append(now)


def record_success(ip: str | None, email: str) -> None:
    with _lock:
        for key in _keys(ip, email):
            _failures.pop(key, None)


def reset() -> None:
    with _lock:
        _failures.clear()
