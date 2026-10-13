"""Durable event -> decision -> native-delivery boundary, without AI or external sends."""
import asyncio
import importlib
import json
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_relay_store as store_tests

store_module = store_tests.store_module

notifications = importlib.import_module("senler_store_tests.senler_notifications")
TARGET = {"platform": "telegram", "chat_id": "12345", "thread_id": None}


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    envelope = store_tests.RelayStoreTests.envelope
    start_event = store_tests.RelayStoreTests.start_event

    def setUp(self):
        store_tests.RelayStoreTests.setUp(self)
        self.store.configure(self.connection, "Notify me only when attention is needed", 0, TARGET)
        self.config = self.store.subscriptions()[self.id]
        self.outbox = notifications.NotificationOutbox(self.store)

    def complete_event(self, result):
        row = self.start_event()
        self.store.append_report(self.id, row["event_id"], row["attempt_id"], result)
        self.store.finish(self.id, row["event_id"], row["attempt_id"])
        return row

    async def deliver(self, sender=None, *, available=True):
        transport = SimpleNamespace(send=sender or AsyncMock(return_value=SimpleNamespace(success=True)))
        runner = SimpleNamespace(config=object(), adapters={})
        modules = {"gateway.config": SimpleNamespace(Platform=lambda name: name),
                   "gateway.delivery": SimpleNamespace(resolve_delivery_transport=lambda *args: transport if available else None)}
        with patch.dict(sys.modules, modules):
            await self.outbox.deliver_one(runner)
        return transport.send

    def make_due(self):
        with self.store.db() as db:
            db.execute("UPDATE notification_queue SET next_try=0")

    async def test_ordinary_event_stays_quiet_and_important_event_delivers_once(self):
        self.complete_event(json.dumps({"notify": False, "summary": "Обычное обсуждение"}))
        sender = await self.deliver()
        sender.assert_not_awaited()
        self.assertEqual(self.store.pending_results(self.id)[0]["failed"], 0)
        self.store.accept(self.config, self.envelope(payload={**self.envelope()["payload"], "event_id": "important"}))
        row = self.store.next_event(self.store.subscriptions())
        self.store.start_processing(self.id, row["event_id"], row["attempt_id"], self.config["local_revision"])
        self.store.append_report(self.id, row["event_id"], row["attempt_id"], '{"notify":true,"summary":"Клиент просит срочно проверить оплату"}')
        self.assertIsNone(self.outbox.claim(), "No intermediate report is delivered")
        self.store.finish(self.id, row["event_id"], row["attempt_id"])
        sender = await self.deliver()
        sender.assert_awaited_once()
        self.assertEqual(sender.await_args.args[:2], ("telegram", "12345"))
        self.assertIn("срочно", sender.await_args.args[2])
        self.assertIn("senler:" + self.id + ":important", sender.await_args.args[2])
        self.store.recover()
        self.store.finish(self.id, row["event_id"], row["attempt_id"])
        await self.deliver(sender)
        sender.assert_awaited_once()
        self.assertEqual(self.outbox.status(self.id)["last_delivery"]["status"], "delivered")

    async def test_offline_then_reconnect_retries_only_saved_summary(self):
        row = self.complete_event('{"notify":true,"summary":"Needs attention"}')
        sender = await self.deliver(available=False)
        sender.assert_not_awaited()
        self.assertEqual(self.outbox.status(self.id)["pending"], 1)
        self.assertIsNone(self.store.next_event(self.store.subscriptions()))
        self.store.accept(self.config, self.envelope())  # Duplicate delivery from Senler.
        self.make_due()
        sender = await self.deliver()
        sender.assert_awaited_once()
        self.assertEqual(self.store.pending_results(self.id)[0]["attempt_id"], row["attempt_id"])
        self.assertEqual(self.outbox.status(self.id)["pending"], 0)

    async def test_timeout_or_crash_does_not_repeat_a_possibly_delivered_message(self):
        self.complete_event('{"notify":true,"summary":"Needs attention"}')
        sender = AsyncMock(side_effect=TimeoutError())
        await self.deliver(sender)
        self.assertEqual(self.outbox.status(self.id)["uncertain"], 1)
        self.store.recover()
        await self.deliver(sender)
        sender.assert_awaited_once()

    async def test_crash_after_claim_preserves_unknown_outcome(self):
        self.complete_event('{"notify":true,"summary":"Needs attention"}')
        self.assertIsNotNone(self.outbox.claim())
        self.store.recover()
        self.assertEqual(self.outbox.status(self.id)["uncertain"], 1)
        sender = await self.deliver()
        sender.assert_not_awaited()

    async def test_review_resolves_only_selected_uncertain_notification(self):
        row = self.complete_event('{"notify":true,"summary":"Saved summary"}')
        await self.deliver(AsyncMock(side_effect=TimeoutError()))
        review = self.outbox.status(self.id)["needs_review"][0]
        self.assertEqual(review["event_id"], row["event_id"])
        self.assertEqual(review["attempts"], 1)
        with self.assertRaises(ValueError):
            self.outbox.resolve("other-subscription", review["id"], "retry")
        self.outbox.resolve(self.id, review["id"], "retry")
        with self.assertRaises(ValueError):
            self.outbox.resolve(self.id, review["id"], "retry")
        sender = await self.deliver()
        sender.assert_awaited_once()
        self.assertIn("Saved summary", sender.await_args.args[2])
        self.assertIsNone(self.store.next_event(self.store.subscriptions()))
        self.outbox.test(self.id)
        await self.deliver(AsyncMock(side_effect=TimeoutError()))
        review = self.outbox.status(self.id)["needs_review"][0]
        self.outbox.resolve(self.id, review["id"], "delivered")
        receipt = self.outbox.status(self.id)["last_test"]
        self.assertEqual(receipt["confirmed_by"], "operator")
        self.assertFalse(self.outbox.status(self.id)["needs_review"])

    async def test_cancelled_sender_and_expired_lease_never_requeue_a_send(self):
        self.complete_event('{"notify":true,"summary":"Needs attention"}')
        entered = asyncio.Event()
        async def wait(*args):
            entered.set()
            await asyncio.Future()
        sender = AsyncMock(side_effect=wait)
        task = asyncio.create_task(self.deliver(sender))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.outbox.status(self.id)["uncertain"], 1)
        self.outbox.test(self.id)
        self.assertIsNotNone(self.outbox.claim())
        self.make_due()
        self.assertIsNone(self.outbox.claim())
        self.assertEqual(self.outbox.status(self.id)["uncertain"], 2)

    async def test_rate_limit_retries_same_text_without_new_processing(self):
        self.complete_event('{"notify":true,"summary":"Needs attention"}')
        sender = AsyncMock(side_effect=[SimpleNamespace(success=False, error_kind="rate_limited", retry_after=30),
                                       SimpleNamespace(success=True)])
        await self.deliver(sender)
        self.assertEqual(self.outbox.status(self.id)["pending"], 1)
        self.make_due()
        await self.deliver(sender)
        self.assertEqual(sender.await_args_list[0], sender.await_args_list[1])
        self.assertIsNone(self.store.next_event(self.store.subscriptions()))

    async def test_test_probe_requires_real_receipt_and_reuses_pending_probe(self):
        first = self.outbox.test(self.id)
        self.assertEqual(first, self.outbox.test(self.id))
        self.assertIsNone(self.outbox.status(self.id)["last_test"])
        sender = await self.deliver()
        sender.assert_awaited_once()
        self.assertEqual(self.outbox.status(self.id)["last_test"]["id"], first)
        self.assertIsNone(self.outbox.status(self.id)["last_delivery"])

    async def test_non_delivery_success_never_becomes_a_receipt(self):
        for result in [{"success": True, "delivered": False}, {"success": True, "skipped": True},
                       {"success": False, "message_id": "partial", "error_kind": "rate_limited"},
                       {"success": False, "error_kind": "rate_limited", "raw_response": {"partial_overflow": True}}]:
            with self.subTest(result=result):
                with self.store.db() as db:
                    db.execute("DELETE FROM notification_queue")
                self.outbox.test(self.id)
                await self.deliver(AsyncMock(return_value=result))
                self.assertIsNone(self.outbox.status(self.id)["last_test"])
                self.assertEqual(self.outbox.status(self.id)["uncertain"], 1)

    async def test_invalid_decision_is_reported_and_never_forwarded(self):
        for result in ['plain narration', '{"notify":"false","summary":"x"}',
                       '{"notify":true,"summary":""}',
                       '{"notify":true,"summary":"x","target":"attacker"}']:
            with self.subTest(result=result):
                self.store.remove(self.id)
                self.store.configure(self.connection, "Monitor", notification_target=TARGET)
                self.config = self.store.subscriptions()[self.id]
                self.complete_event(result)
                sender = await self.deliver()
                sender.assert_not_awaited()
                self.assertEqual(self.store.pending_results(self.id)[0]["failed"], 1)
                self.assertIn("valid notification decision", self.store.pending_results(self.id)[0]["result"])

    def test_target_survives_credentials_refresh_and_server_selection_sync(self):
        self.store.configure({**self.connection, "gateway_secret": "z" * 40}, expected_local_revision=1)
        self.store.sync_settings(self.id, self.envelope(kind="control", active=True, events=["message_new"], revision=1))
        self.assertEqual(self.store.instructions(self.id)["notification_target"], TARGET)

    def test_new_recipient_does_not_receive_an_old_pending_summary(self):
        self.complete_event('{"notify":true,"summary":"Private summary"}')
        self.store.configure(self.connection, expected_local_revision=1, notification_target={**TARGET, "chat_id": "67890"})
        self.assertIsNone(self.outbox.claim())
        self.assertIsNone(self.outbox.status(self.id)["last_test"])

    def test_queue_expiry_is_visible_and_storage_is_bounded(self):
        self.outbox.test(self.id)
        with self.store.db() as db:
            db.execute("UPDATE notification_queue SET created=?", (time.time() - notifications.RETENTION - 1,))
        self.store.cleanup()
        self.assertIsNone(self.outbox.claim())
        self.assertEqual(self.outbox.status(self.id)["last_test"]["status"], "expired")
        with patch.object(notifications, "MAX_NOTIFICATIONS", 2):
            self.outbox.test(self.id)
            with self.store.db() as db, self.assertRaisesRegex(ValueError, "queue is full"):
                notifications.NotificationOutbox.enqueue(db, self.id, "other-test", TARGET, "test", "test")

    def test_status_distinguishes_saved_config_connection_processing_and_delivery(self):
        status = self.store.status()[0]
        self.assertTrue(status["enabled"])
        self.assertFalse(status["connection"]["active_recently"])
        self.assertIsNone(status["last_event"])
        self.store.observe_control(self.id, True, 0)
        self.assertTrue(self.store.status()[0]["connection"]["active_recently"])
        self.complete_event('{"notify":false,"summary":"Quiet"}')
        self.assertEqual(self.store.status()[0]["last_event"]["status"], "completed")
        self.assertIsNone(self.store.status()[0]["notifications"]["last_delivery"])
        self.store.recover()
        self.assertFalse(self.store.status()[0]["connection"]["active_recently"])


if __name__ == "__main__":
    unittest.main()
