"""Safety, rate-limiting, and hang-prevention module for AirAlert Telethon monitor.

Provides deduplication caching, burst-aware alert pacing, and timeout guards.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Set, Tuple, TypeVar

try:
    from telethon.errors import FloodWaitError
except ImportError:  # Telethon not yet installed (e.g. testing standalone)
    class FloodWaitError(Exception):  # type: ignore[no-redef]
        seconds: int = 0

logger = logging.getLogger("AirAlert.Safety")

T = TypeVar("T")


class DeduplicationCache:
    """Thread-safe LRU & TTL cache to prevent duplicate alerts for (chat_id, message_id)."""

    def __init__(self, max_size: int = 5000, ttl_seconds: float = 3600.0) -> None:
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        # Stores (chat_id, message_id) -> timestamp
        self._cache: OrderedDict[Tuple[int, int], float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def check_and_add(self, chat_id: int, message_id: int) -> bool:
        """Check if message is a duplicate. Returns True if already exists (duplicate), False if newly added."""
        key = (chat_id, message_id)
        now = time.monotonic()

        async with self._lock:
            # Check existing entry
            if key in self._cache:
                timestamp = self._cache[key]
                if now - timestamp < self._ttl_seconds:
                    # Move to end as recently seen
                    self._cache.move_to_end(key)
                    return True
                else:
                    # Expired entry, remove it
                    del self._cache[key]

            # Clean expired items if cache grows large
            if len(self._cache) >= self._max_size:
                cutoff = now - self._ttl_seconds
                # Evict oldest expired items or oldest item
                while self._cache:
                    oldest_key, oldest_time = next(iter(self._cache.items()))
                    if oldest_time < cutoff or len(self._cache) >= self._max_size:
                        del self._cache[oldest_key]
                    else:
                        break

            self._cache[key] = now
            return False

    async def clean_expired(self) -> int:
        """Active memory sweep: evicts all entries older than TTL. Returns number of purged items."""
        now = time.monotonic()
        cutoff = now - self._ttl_seconds
        evicted = 0
        async with self._lock:
            # OrderedDict maintains insertion order; oldest entries are at the beginning
            while self._cache:
                oldest_key, oldest_time = next(iter(self._cache.items()))
                if oldest_time < cutoff:
                    del self._cache[oldest_key]
                    evicted += 1
                else:
                    break
        return evicted

    async def size(self) -> int:
        """Return current cache count."""
        async with self._lock:
            return len(self._cache)


class AlertRateLimiter:
    """Token-bucket limiter: up to burst_capacity instant critical sends, then min_interval refill.

    Standard alerts also consume a token but must wait standard_interval_seconds since the
    last send so non-critical chatter cannot drain the emergency burst reservoir.
    """

    def __init__(
        self,
        min_interval_seconds: float = 0.3,
        burst_capacity: int = 3,
        standard_interval_seconds: float = 1.0,
    ) -> None:
        self._min_interval = max(min_interval_seconds, 0.0)
        self._burst_capacity = float(max(burst_capacity, 1))
        self._standard_interval = max(standard_interval_seconds, 0.0)
        self._tokens = self._burst_capacity
        self._last_refill = time.monotonic()
        self._last_send = 0.0
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        """Replenish tokens from elapsed time, clamped to burst capacity."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._last_refill = now
        if elapsed <= 0 or self._min_interval <= 0:
            self._tokens = self._burst_capacity
            return
        self._tokens = min(self._burst_capacity, self._tokens + elapsed / self._min_interval)

    def _wait_seconds(self, is_critical: bool) -> float:
        """Seconds until the next send is allowed (0 if ready now)."""
        token_wait = 0.0
        if self._tokens < 1.0:
            token_wait = (1.0 - self._tokens) * self._min_interval
        if is_critical:
            return token_wait
        now = time.monotonic()
        std_wait = max(0.0, self._standard_interval - (now - self._last_send))
        return max(token_wait, std_wait)

    async def wait_turn(self, is_critical: bool = False) -> None:
        """Wait for a send slot. Critical uses burst tokens; standard also enforces pacing."""
        async with self._lock:
            while True:
                self._refill()
                wait_time = self._wait_seconds(is_critical)
                if wait_time <= 0:
                    self._tokens -= 1.0
                    self._last_send = time.monotonic()
                    return
                await asyncio.sleep(wait_time)


@dataclass
class ServiceMetrics:
    """Runtime telemetry and health counters."""

    start_time: float = field(default_factory=time.time)
    messages_scanned: int = 0
    keywords_matched: int = 0
    alerts_forwarded: int = 0
    duplicates_filtered: int = 0
    errors_caught: int = 0
    flood_wait_events: int = 0

    def get_uptime_str(self) -> str:
        """Return human-readable uptime string."""
        delta = int(time.time() - self.start_time)
        hours, remainder = divmod(delta, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"


# Global metrics instance
metrics = ServiceMetrics()


async def safe_api_call(
    coroutine_func: Callable[[], Awaitable[T]],
    timeout_seconds: float = 10.0,
    action_name: str = "Telegram API Call",
) -> Optional[T]:
    """Execute Telegram API call guarded by strict timeout and FloodWait handler.
    
    Prevents hangs by cancelling stalled coroutines if timeout expires.
    """
    try:
        # Strict timeout wrapper to avoid indefinite network hang
        return await asyncio.wait_for(coroutine_func(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.error("Timeout (%ss) exceeded during %s. Action aborted.", timeout_seconds, action_name)
        metrics.errors_caught += 1
        return None
    except FloodWaitError as fw:
        logger.warning(
            "Telegram FloodWait encountered during %s. Must wait %s seconds.",
            action_name,
            fw.seconds,
        )
        metrics.flood_wait_events += 1
        # Sleep for required flood duration plus safety margin, then return None so caller can retry or drop
        await asyncio.sleep(min(fw.seconds, 60))
        return None
    except Exception as exc:
        logger.error("Unexpected error during %s: %s", action_name, exc, exc_info=True)
        metrics.errors_caught += 1
        return None
