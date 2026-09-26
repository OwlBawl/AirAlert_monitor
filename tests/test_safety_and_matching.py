"""Unit and integration test suite for AirAlert matching and safety systems."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import sys
from pathlib import Path
import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.safety import AlertRateLimiter, AlertSuppressionCache, build_message_dedup_key, normalize_message_for_dedup, safe_api_call
from src.storage import DynamicStore


@pytest.mark.asyncio
async def test_word_boundary_and_tier_matching() -> None:
    """Test unicode word-boundary regex matching and critical tier prioritization."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        kw_file = Path(tmp_dir) / "keywords.json"
        ch_file = Path(tmp_dir) / "channels.json"

        kw_data = {
            "critical": ["ракета", "балістика", "повітряна тривога"],
            "standard": ["дрон", "шахед", "вибух"],
        }
        with open(kw_file, "w", encoding="utf-8") as f:
            json.dump(kw_data, f)

        store = DynamicStore(kw_file, ch_file)
        await store.load_all()

        # 1. Critical whole word match
        res1 = store.match_text("Увага! Зафіксовано пуск ракета в бік області!")
        assert res1 is not None
        assert res1.tier == "critical"
        assert "ракета" in res1.matched_words

        # 2. Multi-word critical match
        res2 = store.match_text("Оголошена Повітряна Тривога по всій території")
        assert res2 is not None
        assert res2.tier == "critical"
        assert "повітряна тривога" in res2.matched_words

        # 3. Standard whole word match
        res3 = store.match_text("Чутно вибух за містом, працює ППО")
        assert res3 is not None
        assert res3.tier == "standard"
        assert "вибух" in res3.matched_words

        # 4. Critical priority over standard (both words in text)
        res4 = store.match_text("Летить дрон та балістика одночасно")
        assert res4 is not None
        assert res4.tier == "critical"
        assert "балістика" in res4.matched_words

        res4b = store.match_text("Увага шахед і балістика!")
        assert res4b is not None
        assert res4b.tier == "critical"
        assert "балістика" in res4b.matched_words

        # 5. Non-matching sub-word (ensure "дрон" does not trigger "ескадрони")
        res5 = store.match_text("Військові ескадрони провели навчання")
        assert res5 is None

        # 6. Punctuation boundary matching
        res6 = store.match_text("Увага: (шахед!) біля кордону.")
        assert res6 is not None
        assert res6.tier == "standard"
        assert "шахед" in res6.matched_words


@pytest.mark.asyncio
async def test_suppression_cache() -> None:
    """Verify atomic keyword reservation plus independent message dedup."""
    cache = AlertSuppressionCache(
        alert_ttl_seconds=0.1,
        cancel_ttl_seconds=0.2,
        message_ttl_seconds=0.3,
    )

    first, reason = await cache.check_and_reserve(("ракета", "дрон"), "critical", "message-a")
    assert first is not None
    assert reason is None
    assert first.words == ("ракета", "дрон")

    blocked, reason = await cache.check_and_reserve(("ракета", "дрон"), "critical", "message-b")
    assert blocked is None
    assert reason == "keyword_cooldown"

    blocked, reason = await cache.check_and_reserve(("шахед",), "critical", "message-a")
    assert blocked is None
    assert reason == "message_dedup"


@pytest.mark.asyncio
async def test_suppression_atomic_concurrency() -> None:
    cache = AlertSuppressionCache(alert_ttl_seconds=60.0, message_ttl_seconds=180.0)

    keyword_results = await asyncio.gather(
        *(
            cache.check_and_reserve(("ракета",), "critical", f"message-{index}")
            for index in range(20)
        )
    )
    assert sum(reservation is not None for reservation, _reason in keyword_results) == 1

    cache = AlertSuppressionCache(alert_ttl_seconds=60.0, message_ttl_seconds=180.0)
    message_results = await asyncio.gather(
        *(
            cache.check_and_reserve((f"keyword-{index}",), "critical", "same-message")
            for index in range(20)
        )
    )
    assert sum(reservation is not None for reservation, _reason in message_results) == 1


@pytest.mark.asyncio
async def test_suppression_old_release_does_not_remove_newer_reservation() -> None:
    cache = AlertSuppressionCache(
        alert_ttl_seconds=0.05,
        cancel_ttl_seconds=0.05,
        message_ttl_seconds=0.05,
    )

    old, _ = await cache.check_and_reserve(("ракета",), "critical", "same-message")
    assert old is not None

    await asyncio.sleep(0.06)
    newer, _ = await cache.check_and_reserve(("ракета",), "critical", "same-message")
    assert newer is not None

    await cache.release(old)
    blocked, reason = await cache.check_and_reserve(("ракета",), "critical", "same-message")
    assert blocked is None
    assert reason == "keyword_cooldown"


@pytest.mark.asyncio
async def test_suppression_cancellation_uses_shared_per_tier_cooldown() -> None:
    cache = AlertSuppressionCache(
        alert_ttl_seconds=0.05,
        cancel_ttl_seconds=0.15,
        message_ttl_seconds=0.01,
    )

    standard, reason = await cache.check_and_reserve(
        ("відбій",), "cancellation_standard", "cancel-standard-a"
    )
    assert standard is not None
    assert reason is None
    assert standard.words == ("відбій",)

    blocked, reason = await cache.check_and_reserve(
        ("скасовано",), "cancellation_standard", "cancel-standard-b"
    )
    assert blocked is None
    assert reason == "keyword_cooldown"

    critical, reason = await cache.check_and_reserve(
        ("відбій",), "cancellation_critical", "cancel-critical-a"
    )
    assert critical is not None
    assert reason is None
    assert critical.words == ("відбій",)

    blocked, reason = await cache.check_and_reserve(
        ("загрозу знято",), "cancellation_critical", "cancel-critical-b"
    )
    assert blocked is None
    assert reason == "keyword_cooldown"

    await asyncio.sleep(0.16)

    standard_again, reason = await cache.check_and_reserve(
        ("інший відбій",), "cancellation_standard", "cancel-standard-c"
    )
    assert standard_again is not None
    assert reason is None

    critical_again, reason = await cache.check_and_reserve(
        ("інше скасування",), "cancellation_critical", "cancel-critical-c"
    )
    assert critical_again is not None
    assert reason is None


@pytest.mark.asyncio
async def test_suppression_old_cancellation_release_does_not_remove_newer_bucket() -> None:
    cache = AlertSuppressionCache(
        alert_ttl_seconds=0.05,
        cancel_ttl_seconds=0.05,
        message_ttl_seconds=0.01,
    )

    old, _ = await cache.check_and_reserve(
        ("відбій",), "cancellation_standard", "cancel-old"
    )
    assert old is not None

    await asyncio.sleep(0.06)
    newer, _ = await cache.check_and_reserve(
        ("скасовано",), "cancellation_standard", "cancel-new"
    )
    assert newer is not None

    await cache.release(old)

    blocked, reason = await cache.check_and_reserve(
        ("ще відбій",), "cancellation_standard", "cancel-third"
    )
    assert blocked is None
    assert reason == "keyword_cooldown"


def test_message_dedup_normalization() -> None:
    base = "🚀 Ракета на Київ!"
    variant = "🧨 РАКЕТА,\nна — Київ? @kyiv_monitor1 https://t.me/source/123"

    assert normalize_message_for_dedup(base) == "ракетанакиїв"
    assert build_message_dedup_key(base) == build_message_dedup_key(variant)
    assert build_message_dedup_key(base) != build_message_dedup_key("2 ракети на Київ!")


def test_message_dedup_text_url_utf16_and_empty_guard() -> None:
    prefix = "🚀 Ракета на Київ! "
    anchor = "Підписатися"
    offset = len(prefix.encode("utf-16-le")) // 2
    length = len(anchor.encode("utf-16-le")) // 2

    assert build_message_dedup_key(
        prefix + anchor,
        ((offset, length),),
    ) == build_message_dedup_key("Ракета на Київ!")

    assert build_message_dedup_key("🚀 @channel https://t.me/source/123!") is None


@pytest.mark.asyncio
async def test_rate_limiter_burst_then_throttle() -> None:
    """Verify 3 instant critical sends, then ~300 ms throttle on the 4th."""
    limiter = AlertRateLimiter(
        min_interval_seconds=0.3,
        burst_capacity=3,
        standard_interval_seconds=1.0,
    )

    start = time.monotonic()
    await limiter.wait_turn(is_critical=True)
    await limiter.wait_turn(is_critical=True)
    await limiter.wait_turn(is_critical=True)
    burst_elapsed = time.monotonic() - start
    assert burst_elapsed < 0.05, f"Burst should be instant, got {burst_elapsed:.4f}s"

    fourth_start = time.monotonic()
    await limiter.wait_turn(is_critical=True)
    fourth_elapsed = time.monotonic() - fourth_start
    assert fourth_elapsed >= 0.25, f"Expected ~0.3s throttle, got {fourth_elapsed:.4f}s"
    assert fourth_elapsed < 0.6, f"Throttle too long: {fourth_elapsed:.4f}s"


@pytest.mark.asyncio
async def test_safe_api_call_timeout_escape() -> None:
    """Verify safe_api_call cancels hung coroutines and returns None without freezing."""
    async def hung_operation() -> str:
        await asyncio.sleep(5.0)
        return "finished"

    start = time.monotonic()
    result = await safe_api_call(hung_operation, timeout_seconds=0.2, action_name="Hung Test")
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 0.8, f"Call did not abort promptly on timeout: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_dynamic_store_modifications() -> None:
    """Verify dynamic keyword and channel add/remove with JSON persistence."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        kw_file = Path(tmp_dir) / "keywords.json"
        ch_file = Path(tmp_dir) / "channels.json"

        store = DynamicStore(kw_file, ch_file)
        await store.load_all()

        # Add keyword
        assert await store.add_keyword("кинджал", tier="critical")
        # Duplicate add returns False
        assert not await store.add_keyword("кинджал", tier="critical")

        keys = await store.get_keywords()
        assert "кинджал" in keys["critical"]

        # Match text with newly added keyword
        match = store.match_text("Запуск кинджал!")
        assert match is not None
        assert match.tier == "critical"

        # Remove keyword
        assert await store.remove_keyword("кинджал")
        assert not await store.remove_keyword("nonexistent")
        assert store.match_text("Запуск кинджал!") is None

        # Add / remove channel
        assert await store.add_channel("@my_channel")
        assert not await store.add_channel("@my_channel")
        assert store.is_channel_monitored(999, "my_channel")
        assert store.is_channel_monitored(999, "@my_channel")

        assert await store.remove_channel("@my_channel")
        assert not store.is_channel_monitored(999, "my_channel")
