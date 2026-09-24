"""Standalone test runner using standard library unittest and asyncio."""

import asyncio
import json
import tempfile
import time
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.bot_manager import _is_sender_admin_event, setup_bot_handlers
from src.config import AppConfig
from src.dispatcher import AlertDispatcher, AlertJob
from src.safety import AlertRateLimiter, AlertSuppressionCache, build_message_dedup_key, normalize_message_for_dedup, safe_api_call
from src.storage import DynamicStore, KeywordMatch


class TestAirAlert(unittest.IsolatedAsyncioTestCase):

    def _command_config(self) -> AppConfig:
        return AppConfig(
            api_id=1,
            api_hash="hash",
            bot_token="token",
            target_chat_id=-1001234567890,
            user_session_name="user",
            bot_session_name="bot",
            keywords_file=Path("keywords.json"),
            channels_file=Path("channels.json"),
        )

    async def test_command_authorization_visible_and_anonymous_admins_only(self) -> None:
        config = self._command_config()
        dispatcher = MagicMock()
        bot = MagicMock()
        bot.get_permissions = AsyncMock()

        event = MagicMock()
        event.chat_id = config.target_chat_id
        event.chat = object()
        event.is_private = False

        # Visible admin is authorized through Telegram permissions.
        event.sender_id = 12345
        bot.get_permissions.return_value = MagicMock(is_admin=True, is_creator=False)
        self.assertTrue(await _is_sender_admin_event(bot, config, dispatcher, event))

        # Anonymous admin may have no sender/from_id in Telethon.
        bot.get_permissions.reset_mock()
        event.sender_id = None
        self.assertTrue(await _is_sender_admin_event(bot, config, dispatcher, event))
        bot.get_permissions.assert_not_awaited()

        # Anonymous admin may also be represented as the same group identity.
        event.sender_id = event.chat_id
        self.assertTrue(await _is_sender_admin_event(bot, config, dispatcher, event))
        bot.get_permissions.assert_not_awaited()

        # Regular member is rejected.
        event.sender_id = 54321
        bot.get_permissions.return_value = MagicMock(is_admin=False, is_creator=False)
        self.assertFalse(await _is_sender_admin_event(bot, config, dispatcher, event))

        # A message sent as an external channel must not be treated as an anonymous admin.
        bot.get_permissions.reset_mock()
        event.sender_id = -1009876543210
        self.assertFalse(await _is_sender_admin_event(bot, config, dispatcher, event))
        bot.get_permissions.assert_not_awaited()

    async def test_id_command_uses_admin_authorization(self) -> None:
        config = self._command_config()
        dispatcher = MagicMock()
        store = MagicMock()
        bot = MagicMock()
        bot.get_permissions = AsyncMock()
        handlers = {}

        def register_handler(_event_spec):
            def decorator(fn):
                handlers[fn.__name__] = fn
                return fn
            return decorator

        bot.on.side_effect = register_handler
        setup_bot_handlers(bot, config, store, dispatcher)
        handle_id = handlers["handle_id"]

        event = MagicMock()
        event.chat_id = config.target_chat_id
        event.chat = object()
        event.is_private = False
        event.sender_id = 11111
        event.reply = AsyncMock()

        bot.get_permissions.return_value = MagicMock(is_admin=False, is_creator=False)
        await handle_id(event)
        event.reply.assert_not_awaited()

        bot.get_permissions.return_value = MagicMock(is_admin=True, is_creator=False)
        await handle_id(event)
        event.reply.assert_awaited_once()

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

    async def test_suppression_cache(self) -> None:
        cache = AlertSuppressionCache(
            alert_ttl_seconds=0.1,
            cancel_ttl_seconds=0.2,
            message_ttl_seconds=0.3,
        )

        first, reason = await cache.check_and_reserve(("ракета", "дрон"), "critical", "message-a")
        self.assertIsNotNone(first)
        self.assertIsNone(reason)
        self.assertEqual(first.words, ("ракета", "дрон"))

        blocked, reason = await cache.check_and_reserve(("ракета", "дрон"), "critical", "message-b")
        self.assertIsNone(blocked)
        self.assertEqual(reason, "keyword_cooldown")

        partial, reason = await cache.check_and_reserve(("ракета", "шахед"), "critical", "message-c")
        self.assertIsNotNone(partial)
        self.assertIsNone(reason)
        self.assertEqual(partial.words, ("шахед",))

        await cache.release(first)
        again, reason = await cache.check_and_reserve(("ракета",), "critical", "message-d")
        self.assertIsNotNone(again)
        self.assertIsNone(reason)
        self.assertEqual(again.words, ("ракета",))

    async def test_suppression_atomic_keyword_concurrency(self) -> None:
        cache = AlertSuppressionCache(alert_ttl_seconds=60.0, message_ttl_seconds=180.0)

        results = await asyncio.gather(
            *(
                cache.check_and_reserve(("ракета",), "critical", f"message-{index}")
                for index in range(20)
            )
        )
        self.assertEqual(sum(reservation is not None for reservation, _reason in results), 1)

    async def test_suppression_atomic_message_dedup_concurrency(self) -> None:
        cache = AlertSuppressionCache(alert_ttl_seconds=60.0, message_ttl_seconds=180.0)

        results = await asyncio.gather(
            *(
                cache.check_and_reserve((f"keyword-{index}",), "critical", "same-message")
                for index in range(20)
            )
        )
        self.assertEqual(sum(reservation is not None for reservation, _reason in results), 1)
        self.assertEqual(
            sum(reason == "message_dedup" for reservation, reason in results if reservation is None),
            19,
        )

    async def test_suppression_policy_precedence_and_independence(self) -> None:
        cache = AlertSuppressionCache(alert_ttl_seconds=60.0, message_ttl_seconds=180.0)

        first, _ = await cache.check_and_reserve(("ракета",), "critical", "hash-a")
        self.assertIsNotNone(first)

        # Same keyword + different message: keyword cooldown blocks first.
        blocked, reason = await cache.check_and_reserve(("ракета",), "critical", "hash-b")
        self.assertIsNone(blocked)
        self.assertEqual(reason, "keyword_cooldown")

        # Different keyword + same message: message dedup blocks.
        blocked, reason = await cache.check_and_reserve(("шахед",), "critical", "hash-a")
        self.assertIsNone(blocked)
        self.assertEqual(reason, "message_dedup")

    async def test_suppression_old_release_does_not_remove_newer_reservation(self) -> None:
        cache = AlertSuppressionCache(
            alert_ttl_seconds=0.05,
            cancel_ttl_seconds=0.05,
            message_ttl_seconds=0.05,
        )

        old, _ = await cache.check_and_reserve(("ракета",), "critical", "same-message")
        self.assertIsNotNone(old)

        await asyncio.sleep(0.06)
        newer, _ = await cache.check_and_reserve(("ракета",), "critical", "same-message")
        self.assertIsNotNone(newer)

        await cache.release(old)

        blocked, reason = await cache.check_and_reserve(("ракета",), "critical", "same-message")
        self.assertIsNone(blocked)
        self.assertEqual(reason, "keyword_cooldown")

    async def test_suppression_cancellation_uses_longer_keyword_ttl(self) -> None:
        cache = AlertSuppressionCache(
            alert_ttl_seconds=0.05,
            cancel_ttl_seconds=0.15,
            message_ttl_seconds=0.01,
        )

        reservation, _ = await cache.check_and_reserve(
            ("відбій",), "cancellation_standard", "cancel-message-a"
        )
        self.assertIsNotNone(reservation)

        await asyncio.sleep(0.06)
        blocked, reason = await cache.check_and_reserve(
            ("відбій",), "cancellation_standard", "cancel-message-b"
        )
        self.assertIsNone(blocked)
        self.assertEqual(reason, "keyword_cooldown")

        await asyncio.sleep(0.10)
        reservation, reason = await cache.check_and_reserve(
            ("відбій",), "cancellation_standard", "cancel-message-c"
        )
        self.assertIsNotNone(reservation)
        self.assertIsNone(reason)

    async def test_message_dedup_ttl_is_independent_from_keyword_ttl(self) -> None:
        cache = AlertSuppressionCache(
            alert_ttl_seconds=0.03,
            cancel_ttl_seconds=0.03,
            message_ttl_seconds=0.12,
        )

        first, _ = await cache.check_and_reserve(("ракета",), "critical", "same-message")
        self.assertIsNotNone(first)

        await asyncio.sleep(0.05)
        blocked, reason = await cache.check_and_reserve(("ракета",), "critical", "same-message")
        self.assertIsNone(blocked)
        self.assertEqual(reason, "message_dedup")

        await asyncio.sleep(0.09)
        again, reason = await cache.check_and_reserve(("ракета",), "critical", "same-message")
        self.assertIsNotNone(again)
        self.assertIsNone(reason)

    async def test_suppression_clean_expired(self) -> None:
        cache = AlertSuppressionCache(
            alert_ttl_seconds=0.05,
            cancel_ttl_seconds=0.1,
            message_ttl_seconds=0.05,
        )
        reservation, _ = await cache.check_and_reserve(
            ("вибух",), "cancellation_standard", "cleanup-message"
        )
        self.assertIsNotNone(reservation)
        self.assertEqual(await cache.size(), 2)

        await asyncio.sleep(0.11)
        purged = await cache.clean_expired()
        self.assertEqual(purged, 2)
        self.assertEqual(await cache.size(), 0)

    def test_message_dedup_normalization(self) -> None:
        base = "🚀 Ракета на Київ!"
        variant = "🧨 РАКЕТА,\nна — Київ?   @kyiv_monitor1 https://t.me/source/123"

        self.assertEqual(
            normalize_message_for_dedup(base),
            "ракетанакиїв",
        )
        self.assertEqual(
            build_message_dedup_key(base),
            build_message_dedup_key(variant),
        )
        self.assertNotEqual(
            build_message_dedup_key(base),
            build_message_dedup_key("2 ракети на Київ!"),
        )

    def test_message_dedup_removes_text_url_anchor_with_utf16_offsets(self) -> None:
        prefix = "🚀 Ракета на Київ! "
        anchor = "Підписатися"
        text = prefix + anchor

        offset = len(prefix.encode("utf-16-le")) // 2
        length = len(anchor.encode("utf-16-le")) // 2

        self.assertEqual(
            build_message_dedup_key(text, ((offset, length),)),
            build_message_dedup_key("Ракета на Київ!"),
        )

    def test_message_dedup_empty_normalization_has_no_key(self) -> None:
        self.assertIsNone(
            build_message_dedup_key("🚀 @channel https://t.me/source/123!")
        )

    async def test_parser_enqueue_failure_releases_full_reservation(self) -> None:
        import datetime
        from src.parser import Channel, setup_parser_handlers

        with tempfile.TemporaryDirectory() as tmp_dir:
            kw_file = Path(tmp_dir) / "keywords.json"
            ch_file = Path(tmp_dir) / "channels.json"

            with open(kw_file, "w", encoding="utf-8") as f:
                json.dump({"critical": ["ракета"], "standard": []}, f)
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

            suppression_cache = AlertSuppressionCache(
                alert_ttl_seconds=3600.0,
                message_ttl_seconds=3600.0,
            )
            dispatcher = MagicMock()
            dispatcher.enqueue.side_effect = [False, True]

            mock_client = MagicMock()
            handlers = []
            mock_client.on.side_effect = lambda event_type: (lambda fn: handlers.append(fn) or fn)
            setup_parser_handlers(mock_client, config, store, suppression_cache, dispatcher)
            handle_message = handlers[0]

            event = MagicMock()
            event.chat_id = -1001111111111
            channel = MagicMock(spec=Channel)
            channel.title = "ППО Радар"
            channel.username = "mon1tor_ua"
            event.chat = channel
            event.message.id = 90001
            event.message.date = datetime.datetime.now(datetime.timezone.utc)
            event.message.entities = []
            event.raw_text = "Ракета на Київ!"
            event.message.message = event.raw_text

            await handle_message(event)
            await handle_message(event)

            self.assertEqual(dispatcher.enqueue.call_count, 2)

    async def test_dispatch_failure_releases_full_reservation(self) -> None:
        cache = AlertSuppressionCache(
            alert_ttl_seconds=3600.0,
            message_ttl_seconds=3600.0,
        )
        reservation, _ = await cache.check_and_reserve(
            ("ракета",), "critical", "dispatch-message"
        )
        self.assertIsNotNone(reservation)

        config = self._command_config()
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=None)
        dispatcher = AlertDispatcher(
            bot_client=bot,
            config=config,
            suppression_cache=cache,
        )
        dispatcher.set_target_entity(object())

        job = AlertJob(
            source_chat_id=-1001111111111,
            source_chat_title="monitor",
            source_chat_username="monitor",
            message_id=90002,
            message_date=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            message_text="Ракета на Київ!",
            match=KeywordMatch(tier="critical", matched_words=("ракета",)),
            suppression_reservation=reservation,
        )

        await dispatcher._dispatch_single_alert(job)

        again, reason = await cache.check_and_reserve(
            ("ракета",), "critical", "dispatch-message"
        )
        self.assertIsNotNone(again)
        self.assertIsNone(reason)

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
            max_message_age_seconds=300.0,
            max_flood_wait_seconds=60.0,
            keyword_cooldown_alert_seconds=60.0,
            keyword_cooldown_cancel_seconds=300.0,
            alert_burst_min_interval_seconds=0.3,
            alert_burst_capacity=3,
            queue_max_size=100,
            log_max_bytes=1024,
            log_backup_count=1,
        )
        dispatcher = AlertDispatcher(bot_client=None, config=config, suppression_cache=AlertSuppressionCache())  # type: ignore[arg-type]
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
            "⚠️\n"
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
        self.assertTrue(msg_cancel_crit.startswith("🟢✅🟢\n"))

        # Cancellation standard test (✅ banner)
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
        self.assertTrue(msg_cancel_std.startswith("✅\n"))

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

            suppression_cache = AlertSuppressionCache(alert_ttl_seconds=3600.0)
            dispatcher = AlertDispatcher(bot_client=None, config=config, suppression_cache=suppression_cache)  # type: ignore[arg-type]

            # Register parser handler on mock client
            mock_client = MagicMock()
            handlers = []
            mock_client.on.side_effect = lambda event_type: (lambda fn: handlers.append(fn) or fn)
            setup_parser_handlers(mock_client, config, store, suppression_cache, dispatcher)
            self.assertTrue(len(handlers) > 0)
            handle_message = handlers[0]

            now = datetime.datetime.now(datetime.timezone.utc)

            # 1. Stale message from 4 hours ago (like 13:21 to 17:33) -> must be dropped
            stale_event = MagicMock()
            stale_event.chat_id = -1001111111111
            stale_channel = MagicMock(spec=Channel)
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
            fresh_channel = MagicMock(spec=Channel)
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
