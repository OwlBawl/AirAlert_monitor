"""Standalone test runner using standard library unittest and asyncio."""

import asyncio
import json
import tempfile
import time
import sys
import unittest
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import AppConfig
from src.dispatcher import AlertDispatcher, AlertJob
from src.safety import AlertRateLimiter, DeduplicationCache, safe_api_call
from src.storage import DynamicStore, KeywordMatch


class TestAirAlert(unittest.IsolatedAsyncioTestCase):

    async def test_word_boundary_and_tier_matching(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            kw_file = Path(tmp_dir) / "keywords.json"
            ch_file = Path(tmp_dir) / "channels.json"

            kw_data = {
                "critical": ["ракета", "балістика", "повітряна тривога", "баліст київ"],
                "standard": ["дрон", "шахед", "вибух", "[бр]"],
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

            res4b = store.match_text("Увага шахед і балістика!")
            self.assertIsNotNone(res4b)
            self.assertEqual(res4b.tier, "critical")
            self.assertIn("балістика", res4b.matched_words)

            # 5. Non-matching sub-word (ensure "дрон" does not trigger "ескадрони")
            res5 = store.match_text("Військові ескадрони провели навчання")
            self.assertIsNone(res5)

            # 7. Multi-word stem combination matching (words anywhere in message with declensions)
            res7a = store.match_text("Зафіксовано рух: балістика летить на київ терміново!")
            self.assertIsNotNone(res7a)
            self.assertEqual(res7a.tier, "critical")
            self.assertIn("баліст київ", res7a.matched_words)

            res7b = store.match_text("Київщина: можлива балістична загроза!")
            self.assertIsNotNone(res7b)
            self.assertEqual(res7b.tier, "critical")
            self.assertIn("баліст київ", res7b.matched_words)

            # Missing one token of the multi-word combination -> should not match
            # (avoid text containing other keyword stems like "дрон" in "дронів")
            res7c = store.match_text("Увага! Київщина під загрозою атаки.")
            self.assertIsNone(res7c)

            # Strict keys match only a full standalone word, case-insensitively.
            res8 = store.match_text("З Брянська на Чернігівщину")
            self.assertIsNone(res8)

            for strict_variant in ("БР", "Бр", "бр", "бР"):
                with self.subTest(strict_variant=strict_variant):
                    res9 = store.match_text(f"2 реактивних {strict_variant}")
                    self.assertIsNotNone(res9)
                    self.assertEqual(res9.tier, "standard")
                    self.assertIn("бр", res9.matched_words)

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

    async def test_deduplication_clean_expired(self) -> None:
        cache = DeduplicationCache(max_size=500, ttl_seconds=0.2)
        await cache.check_and_add(1, 100)
        await cache.check_and_add(1, 101)
        self.assertEqual(await cache.size(), 2)

        # Wait for expiry
        await asyncio.sleep(0.25)
        purged = await cache.clean_expired()
        self.assertEqual(purged, 2)
        self.assertEqual(await cache.size(), 0)

    async def test_rate_limiter_burst_and_spacing(self) -> None:
        limiter = AlertRateLimiter(
            min_interval_seconds=0.3,
            burst_capacity=3,
            standard_interval_seconds=1.0,
        )

        # 1. First priority send is immediate
        t0 = time.monotonic()
        await limiter.wait_turn(is_critical=True)
        self.assertLess(time.monotonic() - t0, 0.05)

        # 2. Subsequent priority sends in burst are spaced by 0.3s
        t1 = time.monotonic()
        await limiter.wait_turn(is_critical=True)
        e1 = time.monotonic() - t1
        self.assertGreaterEqual(e1, 0.25)
        self.assertLess(e1, 0.45)

        t2 = time.monotonic()
        await limiter.wait_turn(is_critical=True)
        e2 = time.monotonic() - t2
        self.assertGreaterEqual(e2, 0.25)
        self.assertLess(e2, 0.45)

        # 3. Standard send: after 1.0s has passed, sent immediately
        await asyncio.sleep(1.05)
        t3 = time.monotonic()
        await limiter.wait_turn(is_critical=False)
        self.assertLess(time.monotonic() - t3, 0.05)

        # 4. Rapid standard send: must wait remainder of 1.0s interval
        t4 = time.monotonic()
        await limiter.wait_turn(is_critical=False)
        e4 = time.monotonic() - t4
        self.assertGreaterEqual(e4, 0.90)
        self.assertLess(e4, 1.20)

    def test_dispatcher_send_target_cache_and_fallback(self) -> None:
        config = AppConfig(
            api_id=1,
            api_hash="hash",
            bot_token="token",
            target_chat_id=-1001234567890,
            user_session_name="user",
            bot_session_name="bot",
            keywords_file=Path("keywords.json"),
            channels_file=Path("channels.json"),
            alert_interval_seconds=1.0,
            api_timeout_seconds=10.0,
            heartbeat_interval_seconds=30.0,
            dedup_ttl_seconds=3600.0,
            dedup_max_size=500,
            queue_max_size=100,
            log_max_bytes=1024,
            log_backup_count=1,
        )
        dispatcher = AlertDispatcher(bot_client=None, config=config)  # type: ignore[arg-type]
        self.assertEqual(dispatcher.send_target(), config.target_chat_id)

        cached = object()
        dispatcher.set_target_entity(cached)
        self.assertIs(dispatcher.send_target(), cached)

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

            self.assertTrue(await store.add_keyword("[бр]"))
            keys = await store.get_keywords()
            self.assertIn("[бр]", keys["standard"])
            self.assertIsNone(store.match_text("Брянська область"))
            self.assertIsNotNone(store.match_text("БР рухається"))

            match = store.match_text("Запуск кинджал!")
            self.assertIsNotNone(match)
            self.assertEqual(match.tier, "critical")

            # Strict removal by tier
            self.assertFalse(await store.remove_keyword("кинджал", tier="standard"))
            self.assertTrue(await store.remove_keyword("кинджал", tier="critical"))
            self.assertFalse(await store.remove_keyword("nonexistent", tier="critical"))
            self.assertIsNone(store.match_text("Запуск кинджал!"))

            # Add / remove negative keyword
            self.assertTrue(await store.add_keyword("-каб", tier="standard"))
            self.assertFalse(await store.remove_keyword("-каб", tier="critical"))
            self.assertTrue(await store.remove_keyword("-каб", tier="standard"))

            # Add / remove channel
            self.assertTrue(await store.add_channel("@my_channel"))
            self.assertFalse(await store.add_channel("@my_channel"))
            self.assertTrue(store.is_channel_monitored(999, "my_channel"))
            self.assertTrue(store.is_channel_monitored(999, "@my_channel"))

            self.assertTrue(await store.remove_channel("@my_channel"))
            self.assertFalse(store.is_channel_monitored(999, "my_channel"))

    async def test_negative_keywords_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            kw_file = Path(tmp_dir) / "keywords.json"
            ch_file = Path(tmp_dir) / "channels.json"

            kw_data = {
                "critical": ["крилат"],
                "critical_negative": ["навчання"],
                "standard": ["пуск"],
                "standard_negative": ["каб"],
                "cancellation": [],
                "cancellation_negative": [],
            }
            with open(kw_file, "w", encoding="utf-8") as f:
                json.dump(kw_data, f)

            store = DynamicStore(kw_file, ch_file)
            await store.load_all()

            # Positive critical match
            m1 = store.match_text("Пуски крилатих ракет з бортів Ту-95")
            self.assertIsNotNone(m1)
            self.assertEqual(m1.tier, "critical")

            # Blocked critical match by negative stop-word
            m2 = store.match_text("Пуски крилатих ракет (навчання екіпажів)")
            self.assertIsNone(m2)

            # Positive standard match
            m3 = store.match_text("Зафіксовано пуск невідомої цілі")
            self.assertIsNotNone(m3)
            self.assertEqual(m3.tier, "standard")

            # Blocked standard match by negative stop-word
            m4 = store.match_text("Пуски КАБ у напрямку Харкова")
            self.assertIsNone(m4)

    async def test_cancellation_tier_and_negation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            kw_file = Path(tmp_dir) / "keywords.json"
            ch_file = Path(tmp_dir) / "channels.json"

            kw_data = {
                "critical": ["балісти"],
                "critical_negative": [],
                "standard": ["шахед"],
                "standard_negative": [],
                "cancellation": ["відбій", "чисто"],
                "cancellation_negative": ["очікуємо"],
            }
            with open(kw_file, "w", encoding="utf-8") as f:
                json.dump(kw_data, f)

            store = DynamicStore(kw_file, ch_file)
            await store.load_all()

            # Critical cancellation: cancellation key + critical threat mentioned
            m_crit_cancel = store.match_text("Відбій загрози балістики для центральних областей")
            self.assertIsNotNone(m_crit_cancel)
            self.assertEqual(m_crit_cancel.tier, "cancellation_critical")
            self.assertIn("відбій", m_crit_cancel.matched_words)

            # Standard cancellation: cancellation key without critical threat
            m_std_cancel = store.match_text("Відбій тривоги у Києві та області")
            self.assertIsNotNone(m_std_cancel)
            self.assertEqual(m_std_cancel.tier, "cancellation_standard")

            # Cancellation negated by cancellation stop-word
            m_neg_cancel = store.match_text("Відбій по шахедах, але очікуємо пусків з моря")
            self.assertIsNone(m_neg_cancel)

    def test_alert_formatting(self) -> None:
        import datetime

        # Standard tier test (no top banner)
        std_job = AlertJob(
            source_chat_id=-1001234567890,
            source_chat_title="monitor",
            source_chat_username="war_monitor",
            message_id=43888,
            message_date=datetime.datetime.now(datetime.timezone.utc),
            message_text="🅿️ 2х мгКР Бандероль вектор Переяслав, далі Обухів.",
            match=KeywordMatch(tier="standard", matched_words=["бандероль"]),
        )
        msg_text = AlertDispatcher.format_alert(std_job)
        expected_std = (
            "<blockquote>🅿️ 2х мгКР Бандероль вектор Переяслав, далі Обухів.</blockquote>\n"
            "📢 monitor: бандероль\n"
            "🔗 https://t.me/war_monitor/43888"
        )
        self.assertEqual(msg_text, expected_std)

        # Critical tier test (‼️🚨‼️ banner)
        crit_job = AlertJob(
            source_chat_id=-1001234567890,
            source_chat_title="monitor",
            source_chat_username="war_monitor",
            message_id=43889,
            message_date=datetime.datetime.now(datetime.timezone.utc),
            message_text="Пуск балістики на Київ!",
            match=KeywordMatch(tier="critical", matched_words=["балістика"]),
        )
        msg_text_crit = AlertDispatcher.format_alert(crit_job)
        expected_crit = (
            "‼️🚨‼️\n"
            "<blockquote>Пуск балістики на Київ!</blockquote>\n"
            "📢 monitor: балістика\n"
            "🔗 https://t.me/war_monitor/43889"
        )
        self.assertEqual(msg_text_crit, expected_crit)

        # Cancellation critical test (🟡⚠️🟡 banner)
        cancel_crit_job = AlertJob(
            source_chat_id=-1001234567890,
            source_chat_title="monitor",
            source_chat_username="war_monitor",
            message_id=43890,
            message_date=datetime.datetime.now(datetime.timezone.utc),
            message_text="Відбій загрози балістики!",
            match=KeywordMatch(tier="cancellation_critical", matched_words=["відбій"]),
        )
        msg_cancel_crit = AlertDispatcher.format_alert(cancel_crit_job)
        self.assertTrue(msg_cancel_crit.startswith("🟡⚠️🟡\n"))

        # Cancellation standard test (🟢✅🟢 banner)
        cancel_std_job = AlertJob(
            source_chat_id=-1001234567890,
            source_chat_title="monitor",
            source_chat_username="war_monitor",
            message_id=43891,
            message_date=datetime.datetime.now(datetime.timezone.utc),
            message_text="Відбій повітряної тривоги.",
            match=KeywordMatch(tier="cancellation_standard", matched_words=["відбій"]),
        )
        msg_cancel_std = AlertDispatcher.format_alert(cancel_std_job)
        self.assertTrue(msg_cancel_std.startswith("🟢✅🟢\n"))

    async def test_message_age_guard_drops_stale_messages(self) -> None:
        import datetime
        from unittest.mock import MagicMock
        from src.parser import Channel, setup_parser_handlers
        from src.safety import metrics

        with tempfile.TemporaryDirectory() as tmp_dir:
            kw_file = Path(tmp_dir) / "keywords.json"
            ch_file = Path(tmp_dir) / "channels.json"

            with open(kw_file, "w", encoding="utf-8") as f:
                json.dump({"critical": ["дарниц"], "standard": []}, f)
            with open(ch_file, "w", encoding="utf-8") as f:
                json.dump({"channels": ["@mon1tor_ua"]}, f)

            store = DynamicStore(kw_file, ch_file)
            await store.load_all()

            config = AppConfig(
                api_id=1,
                api_hash="hash",
                bot_token="token",
                target_chat_id=-1001234567890,
                user_session_name="user",
                bot_session_name="bot",
                keywords_file=kw_file,
                channels_file=ch_file,
                max_message_age_seconds=300.0,
            )

            dedup = DeduplicationCache(max_size=100, ttl_seconds=3600.0)
            dispatcher = AlertDispatcher(bot_client=None, config=config)  # type: ignore[arg-type]

            # Register parser handler on mock client
            mock_client = MagicMock()
            handlers = []
            mock_client.on.side_effect = lambda event_type: (lambda fn: handlers.append(fn) or fn)
            setup_parser_handlers(mock_client, config, store, dedup, dispatcher)
            self.assertTrue(len(handlers) > 0)
            handle_message = handlers[0]

            now = datetime.datetime.now(datetime.timezone.utc)

            # 1. Stale message from 4 hours ago (like 13:21 to 17:33) -> must be dropped
            stale_event = MagicMock()
            stale_event.chat_id = -1001111111111
            stale_channel = Channel()
            stale_channel.title = "ППО Радар"
            stale_channel.username = "mon1tor_ua"
            stale_event.chat = stale_channel
            stale_event.message.id = 74102
            stale_event.message.date = now - datetime.timedelta(hours=4)
            stale_event.raw_text = "Ракета-дрон Герань-5 на Дарницю, ДВРЗ."
            stale_event.message.message = stale_event.raw_text

            initial_stale_count = metrics.stale_messages_dropped
            initial_qsize = dispatcher.queue.qsize()

            await handle_message(stale_event)

            self.assertEqual(metrics.stale_messages_dropped, initial_stale_count + 1)
            self.assertEqual(dispatcher.queue.qsize(), initial_qsize)

            # 2. Fresh message from 30 seconds ago -> must be enqueued
            fresh_event = MagicMock()
            fresh_event.chat_id = -1001111111111
            fresh_channel = Channel()
            fresh_channel.title = "ППО Радар"
            fresh_channel.username = "mon1tor_ua"
            fresh_event.chat = fresh_channel
            fresh_event.message.id = 74103
            fresh_event.message.date = now - datetime.timedelta(seconds=30)
            fresh_event.raw_text = "Нова ракета на Дарницю!"
            fresh_event.message.message = fresh_event.raw_text

            await handle_message(fresh_event)

            self.assertEqual(dispatcher.queue.qsize(), initial_qsize + 1)


if __name__ == "__main__":
    unittest.main()
