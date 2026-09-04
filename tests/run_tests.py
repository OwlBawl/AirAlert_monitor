"""Standalone test runner using standard library unittest and asyncio."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from safety import AlertRateLimiter, DeduplicationCache, safe_api_call
from storage import DynamicStore


class TestAirAlert(unittest.IsolatedAsyncioTestCase):

    async def test_word_boundary_and_tier_matching(self) -> None:
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
            self.assertIsNotNone(res1)
            self.assertEqual(res1.tier, "critical")
            self.assertIn("ракета", res1.matched_words)

            # 2. Multi-word critical match
            res2 = store.match_text("Оголошена Повітряна Тривога по всій території")
            self.assertIsNotNone(res2)
            self.assertEqual(res2.tier, "critical")
            self.assertIn("повітряна тривога", res2.matched_words)

            # 3. Standard whole word match
            res3 = store.match_text("Чутно вибух за містом, працює ППО")
            self.assertIsNotNone(res3)
            self.assertEqual(res3.tier, "standard")
            self.assertIn("вибух", res3.matched_words)

            # 4. Critical priority over standard (both words in text)
            res4 = store.match_text("Летить дрон та балістика одночасно")
            self.assertIsNotNone(res4)
            self.assertEqual(res4.tier, "critical")
            self.assertIn("балістика", res4.matched_words)

            # 5. Non-matching sub-word (ensure "дрон" does not trigger "ескадрони")
            res5 = store.match_text("Військові ескадрони провели навчання")
            self.assertIsNone(res5)

            # 6. Punctuation boundary matching
            res6 = store.match_text("Увага: (шахед!) біля кордону.")
            self.assertIsNotNone(res6)
            self.assertEqual(res6.tier, "standard")
            self.assertIn("шахед", res6.matched_words)

    async def test_deduplication_cache(self) -> None:
        cache = DeduplicationCache(max_size=10, ttl_seconds=0.5)

        # First addition -> not duplicate
        is_dup1 = await cache.check_and_add(1001, 555)
        self.assertFalse(is_dup1)

        # Second addition immediate -> is duplicate
        is_dup2 = await cache.check_and_add(1001, 555)
        self.assertTrue(is_dup2)

        # Different message_id -> not duplicate
        is_dup3 = await cache.check_and_add(1001, 556)
        self.assertFalse(is_dup3)

        # Wait for TTL expiry
        await asyncio.sleep(0.6)
        is_dup4 = await cache.check_and_add(1001, 555)
        self.assertFalse(is_dup4)

    async def test_rate_limiter_pacing(self) -> None:
        interval = 0.15
        limiter = AlertRateLimiter(min_interval_seconds=interval)

        start = time.monotonic()
        await limiter.wait_turn()
        await limiter.wait_turn()
        await limiter.wait_turn()
        elapsed = time.monotonic() - start

        self.assertGreaterEqual(elapsed, 0.28)

    async def test_safe_api_call_timeout_escape(self) -> None:
        async def hung_operation() -> str:
            await asyncio.sleep(5.0)
            return "finished"

        start = time.monotonic()
        result = await safe_api_call(hung_operation, timeout_seconds=0.2, action_name="Hung Test")
        elapsed = time.monotonic() - start

        self.assertIsNone(result)
        self.assertLess(elapsed, 0.8)

    async def test_dynamic_store_modifications(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            kw_file = Path(tmp_dir) / "keywords.json"
            ch_file = Path(tmp_dir) / "channels.json"

            store = DynamicStore(kw_file, ch_file)
            await store.load_all()

            # Add keyword
            self.assertTrue(await store.add_keyword("кинджал", tier="critical"))
            self.assertFalse(await store.add_keyword("кинджал", tier="critical"))

            keys = await store.get_keywords()
            self.assertIn("кинджал", keys["critical"])

            match = store.match_text("Запуск кинджал!")
            self.assertIsNotNone(match)
            self.assertEqual(match.tier, "critical")

            # Remove keyword
            self.assertTrue(await store.remove_keyword("кинджал"))
            self.assertFalse(await store.remove_keyword("nonexistent"))
            self.assertIsNone(store.match_text("Запуск кинджал!"))

            # Add / remove channel
            self.assertTrue(await store.add_channel("@my_channel"))
            self.assertFalse(await store.add_channel("@my_channel"))
            self.assertTrue(store.is_channel_monitored(999, "my_channel"))
            self.assertTrue(store.is_channel_monitored(999, "@my_channel"))

            self.assertTrue(await store.remove_channel("@my_channel"))
            self.assertFalse(store.is_channel_monitored(999, "my_channel"))


if __name__ == "__main__":
    unittest.main()
