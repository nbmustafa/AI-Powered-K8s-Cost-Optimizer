"""Simple in-memory TTL cache for K8s snapshots and AI responses."""

import time
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MetricsCache:
    def __init__(self, ttl_seconds: int = 300):
        self._store: dict = {}
        self._default_ttl = ttl_seconds

    def set(self, key: str, value: Any, ttl_override: Optional[int] = None) -> None:
        ttl = ttl_override if ttl_override is not None else self._default_ttl
        self._store[key] = {
            "value": value,
            "expires_at": time.monotonic() + ttl,
        }
        logger.debug(f"Cache SET: {key} (TTL={ttl}s)")

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if not entry:
            return None
        if time.monotonic() > entry["expires_at"]:
            del self._store[key]
            logger.debug(f"Cache EXPIRED: {key}")
            return None
        logger.debug(f"Cache HIT: {key}")
        return entry["value"]

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def stats(self) -> dict:
        now = time.monotonic()
        return {
            "total_keys": len(self._store),
            "live_keys": sum(1 for e in self._store.values() if e["expires_at"] > now),
            "expired_keys": sum(1 for e in self._store.values() if e["expires_at"] <= now),
        }
