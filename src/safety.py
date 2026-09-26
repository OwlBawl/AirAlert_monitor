"""Safety, rate-limiting, and hang-prevention module for AirAlert Telethon monitor.

Provides keyword debounce caching, burst-aware alert pacing, and timeout guards.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Set, Tuple, TypeVar, Iterable

try:
    from telethon.errors import FloodWaitError
except ImportError:  # Telethon not yet installed (e.g. testing standalone)
    class FloodWaitError(Exception):  # type: ignore[no-redef]
        seconds: int = 0

logger = logging.getLogger("AirAlert.Safety")

T = TypeVar("T")


# Dedup normalization removes only the presentation/source differences explicitly
# approved for AirAlert. Other punctuation remains meaningful.
_URL_RE = re.compile(
    r"(?i)(?<!\w)(?:https?://|www\.|(?:t|telegram)\.me/)\S+"
)
_USERNAME_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]+\b")
_KEYCAP_EMOJI_RE = re.compile("[#*0-9]\ufe0f?\u20e3")
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\u2600-\u27BF"
    "\uFE0E\uFE0F"
    "\u200D"
    "]"
)
_DEDUP_PUNCTUATION_RE = re.compile(r"[!.,?\-–—−]")
_WHITESPACE_RE = re.compile(r"\s+")


def _remove_utf16_spans(
    text: str,
    spans: Iterable[Tuple[int, int]],
) -> str:
    """Remove Telegram entity spans expressed as UTF-16 code-unit offset/length pairs."""
    encoded = text.encode("utf-16-le")
    raw_spans: list[Tuple[int, int]] = []

    for offset, length in spans:
        if offset < 0 or length <= 0:
            continue
        start = offset * 2
        end = min((offset + length) * 2, len(encoded))
        if start < len(encoded) and start < end:
            raw_spans.append((start, end))

    if not raw_spans:
        return text

    # Merge overlaps first, then remove from the end so original Telegram
    # UTF-16 offsets remain valid even when emoji precede a hyperlink.
    merged: list[list[int]] = []
    for start, end in sorted(raw_spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    for start, end in reversed(merged):
        encoded = encoded[:start] + encoded[end:]

    try:
        return encoded.decode("utf-16-le")
    except UnicodeDecodeError:
        # Telegram entities should align to UTF-16 boundaries. Keep original text
        # rather than corrupting content if malformed metadata is ever received.
        return text


def normalize_message_for_dedup(
    text: str,
    text_url_spans: Iterable[Tuple[int, int]] = (),
) -> str:
    """Normalize full message text for message deduplication only."""
    normalized = _remove_utf16_spans(text, text_url_spans)
    normalized = _URL_RE.sub("", normalized)
    normalized = _USERNAME_RE.sub("", normalized)
    normalized = _KEYCAP_EMOJI_RE.sub("", normalized)
    normalized = _EMOJI_RE.sub("", normalized)
    normalized = normalized.casefold()
    normalized = _DEDUP_PUNCTUATION_RE.sub("", normalized)
    normalized = _WHITESPACE_RE.sub("", normalized)
    return normalized


def build_message_dedup_key(
    text: str,
    text_url_spans: Iterable[Tuple[int, int]] = (),
) -> Optional[str]:
    """Return SHA-256 of normalized meaningful text, or None when nothing remains."""
    normalized = normalize_message_for_dedup(text, text_url_spans)
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SuppressionReservation:
    """Suppression entries atomically reserved by one parser processing flow."""

    keyword_entries: Tuple[Tuple[str, float], ...]
    matched_words: Tuple[str, ...]
    cancellation_entry: Optional[Tuple[str, float]] = None
    message_entry: Optional[Tuple[str, float]] = None

    @property
    def words(self) -> Tuple[str, ...]:
        """Return real matched keywords for alert formatting/logging."""
        return self.matched_words


class AlertSuppressionCache:
    """Atomic active-keyword cooldown, cancellation-tier cooldown, and message dedup."""

    def __init__(
        self,
        alert_ttl_seconds: float = 60.0,
        cancel_ttl_seconds: float = 300.0,
        message_ttl_seconds: float = 180.0,
    ) -> None:
        self._alert_ttl = alert_ttl_seconds
        self._cancel_ttl = cancel_ttl_seconds
        self._message_ttl = message_ttl_seconds
        self._keyword_cache: OrderedDict[str, float] = OrderedDict()
        self._cancellation_cache: OrderedDict[str, float] = OrderedDict()
        self._message_cache: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def check_and_reserve(
        self,
        words: Iterable[str],
        tier: str,
        message_key: Optional[str] = None,
    ) -> Tuple[Optional[SuppressionReservation], Optional[str]]:
        """Atomically check suppression policies and reserve the applicable free keys."""
        normalized_words: list[str] = []
        seen: set[str] = set()
        for word in words:
            word_clean = word.strip().lower()
            if not word_clean or word_clean in seen:
                continue
            seen.add(word_clean)
            normalized_words.append(word_clean)

        if not normalized_words:
            return None, "keyword_cooldown"

        is_cancellation = tier.startswith("cancellation")

        async with self._lock:
            now = time.monotonic()
            keyword_entries: list[Tuple[str, float]] = []
            cancellation_entry: Optional[Tuple[str, float]] = None

            if is_cancellation:
                # Cancellation cooldown is shared by tier, not by cancellation key.
                # cancellation_critical and cancellation_standard are independent buckets.
                cached_at = self._cancellation_cache.get(tier)
                if cached_at is not None:
                    if now - cached_at < self._cancel_ttl:
                        return None, "keyword_cooldown"
                    del self._cancellation_cache[tier]
                accepted_words = tuple(normalized_words)
            else:
                # Active critical/standard alerts keep independent per-key cooldowns.
                uncooled: list[str] = []
                for word_clean in normalized_words:
                    cached_at = self._keyword_cache.get(word_clean)
                    if cached_at is not None:
                        if now - cached_at < self._alert_ttl:
                            continue
                        del self._keyword_cache[word_clean]
                    uncooled.append(word_clean)

                if not uncooled:
                    return None, "keyword_cooldown"
                accepted_words = tuple(uncooled)

            # Keyword/tier cooldown is checked first. Only an eligible candidate
            # reaches message dedup, so rejected candidates reserve nothing.
            if message_key is not None:
                message_cached_at = self._message_cache.get(message_key)
                if message_cached_at is not None:
                    if now - message_cached_at < self._message_ttl:
                        return None, "message_dedup"
                    del self._message_cache[message_key]

            if is_cancellation:
                self._cancellation_cache[tier] = now
                cancellation_entry = (tier, now)
            else:
                for word_clean in accepted_words:
                    self._keyword_cache[word_clean] = now
                    keyword_entries.append((word_clean, now))

            message_entry: Optional[Tuple[str, float]] = None
            if message_key is not None:
                self._message_cache[message_key] = now
                message_entry = (message_key, now)

            return (
                SuppressionReservation(
                    keyword_entries=tuple(keyword_entries),
                    matched_words=accepted_words,
                    cancellation_entry=cancellation_entry,
                    message_entry=message_entry,
                ),
                None,
            )

    async def reset_cancellation_for_active_tier(self, tier: str) -> bool:
        """Clear the matching cancellation-tier cooldown for an accepted active alert."""
        cancellation_tier = {
            "standard": "cancellation_standard",
            "critical": "cancellation_critical",
        }.get(tier)
        if cancellation_tier is None:
            return False

        async with self._lock:
            return self._cancellation_cache.pop(cancellation_tier, None) is not None

    async def release(self, reservation: Optional[SuppressionReservation]) -> None:
        """Release only entries still owned by this processing flow."""
        if reservation is None:
            return

        async with self._lock:
            for word_clean, reserved_at in reservation.keyword_entries:
                if self._keyword_cache.get(word_clean) == reserved_at:
                    del self._keyword_cache[word_clean]

            if reservation.cancellation_entry is not None:
                tier, reserved_at = reservation.cancellation_entry
                if self._cancellation_cache.get(tier) == reserved_at:
                    del self._cancellation_cache[tier]

            if reservation.message_entry is not None:
                message_key, reserved_at = reservation.message_entry
                if self._message_cache.get(message_key) == reserved_at:
                    del self._message_cache[message_key]

    async def clean_expired(self) -> int:
        """Evict expired active-keyword, cancellation-tier, and message-dedup entries."""
        now = time.monotonic()
        evicted = 0
        keyword_cutoff = now - self._alert_ttl
        cancellation_cutoff = now - self._cancel_ttl
        message_cutoff = now - self._message_ttl

        async with self._lock:
            while self._keyword_cache:
                oldest_key, oldest_time = next(iter(self._keyword_cache.items()))
                if oldest_time < keyword_cutoff:
                    del self._keyword_cache[oldest_key]
                    evicted += 1
                else:
                    break

            while self._cancellation_cache:
                oldest_tier, oldest_time = next(iter(self._cancellation_cache.items()))
                if oldest_time < cancellation_cutoff:
                    del self._cancellation_cache[oldest_tier]
                    evicted += 1
                else:
                    break

            while self._message_cache:
                oldest_key, oldest_time = next(iter(self._message_cache.items()))
                if oldest_time < message_cutoff:
                    del self._message_cache[oldest_key]
                    evicted += 1
                else:
                    break

        return evicted

    async def size(self) -> int:
        """Return total number of active-keyword, cancellation-tier, and message entries."""
        async with self._lock:
            return (
                len(self._keyword_cache)
                + len(self._cancellation_cache)
                + len(self._message_cache)
            )


class AlertRateLimiter:
    """Token-bucket limiter: up to burst_capacity priority sends with min_interval spacing, then throttled.

    Standard alerts enforce standard_interval_seconds since the last dispatched alert
    (sent immediately if >= standard_interval_seconds has elapsed since last message).
    Priority alerts can burst up to burst_capacity messages spaced by min_interval_seconds
    (sent immediately if >= min_interval_seconds has elapsed since last message).
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
        if elapsed <= 0:
            return
        refill_rate = 1.0 / self._standard_interval if self._standard_interval > 0 else 10.0
        self._tokens = min(self._burst_capacity, self._tokens + elapsed * refill_rate)

    def _wait_seconds(self, is_critical: bool) -> float:
        """Seconds until the next send is allowed (0 if ready now)."""
        now = time.monotonic()
        elapsed_since_send = now - self._last_send
        if is_critical:
            interval_wait = max(0.0, self._min_interval - elapsed_since_send)
            token_wait = 0.0
            if self._tokens < 1.0:
                token_wait = (1.0 - self._tokens) * self._standard_interval
            return max(interval_wait, token_wait)
        else:
            return max(0.0, self._standard_interval - elapsed_since_send)

    async def wait_turn(self, is_critical: bool = False) -> None:
        """Wait for a send slot. Critical uses burst tokens with spacing; standard enforces pacing."""
        async with self._lock:
            while True:
                self._refill()
                wait_time = self._wait_seconds(is_critical)
                if wait_time <= 0.001:
                    if is_critical:
                        self._tokens = max(0.0, self._tokens - 1.0)
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
    keyword_cooldown_filtered: int = 0
    message_dedup_filtered: int = 0
    stale_messages_dropped: int = 0
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
    max_flood_wait_seconds: float = 60.0,
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
        await asyncio.sleep(min(fw.seconds, max_flood_wait_seconds))
        return None
    except Exception as exc:
        logger.error("Unexpected error during %s: %s", action_name, exc, exc_info=True)
        metrics.errors_caught += 1
        return None
