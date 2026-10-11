"""Adapter boundary tests with a deterministic gateway, without model or network calls."""
import asyncio
from contextvars import copy_context
from enum import Enum
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class Outcome(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class FakeGatewayBase:
    def __init__(self, config, platform):
        self.platform = platform

    def build_source(self, **values):
        return SimpleNamespace(**values)

    def _event_session_key(self, event):
        return event.message_id

    async def cancel_session_processing(self, session):
        return None

    async def handle_message(self, event):
        self.seen = event
        event._gateway_accepted = True
        self.session_end(completed=True, failed=False, interrupted=False, platform="senler-io")
        await self.send(event.source.chat_id, "Prepared draft")
        await self.on_processing_complete(event, Outcome.SUCCESS)
        self._running = False


class RelayAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway_config = {}
        modules = {
            "gateway.config": SimpleNamespace(Platform=lambda name: name),
            "gateway.platforms.base": SimpleNamespace(BasePlatformAdapter=FakeGatewayBase, SendResult=SimpleNamespace),
            "gateway.platforms.event": SimpleNamespace(MessageEvent=SimpleNamespace, ProcessingOutcome=Outcome),
            "gateway.relay.ws_transport": SimpleNamespace(WebSocketRelayTransport=object),
            "hermes_constants": SimpleNamespace(get_hermes_home=lambda: Path(self.temp.name)),
            "hermes_cli.config": SimpleNamespace(load_config_readonly=lambda: self.gateway_config),
            "agent.secret_scope": SimpleNamespace(UnscopedSecretError=RuntimeError, get_secret=lambda name: None),
        }
        spec = importlib.util.spec_from_file_location("senler_adapter_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        plugin = importlib.util.module_from_spec(spec)
        self.plugin = plugin
        module_patch = patch.dict(sys.modules, {**modules, spec.name: plugin})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        spec.loader.exec_module(plugin)
        self.adapter = plugin.create_relay_adapter({})
        store_module = sys.modules["senler_adapter_test.senler_relay_store"]
        self.actions = sys.modules["senler_adapter_test.senler_relay_actions"]
        self.relay = sys.modules["senler_adapter_test.senler_relay"]
        self.adapter.session_end = self.actions.on_session_end
        self.store = store_module.RelayStore(self.temp.name)
        self.id = "123456789012345678901234"
        self.project = str(uuid4())
        connection = {"webhook_id": self.id, "project_id": self.project, "relay_url": "wss://api.example.test/api/hermes-relay/" + self.id,
            "gateway_id": self.id, "gateway_secret": "x" * 40, "events": ["message_new"], "revision": 0}
        self.store.configure(connection, "Prepare a draft only")
        self.config = self.store.subscriptions()[self.id]
        self.adapter.store = self.store
        self.adapter.policy = "Use the configured project"
        self.adapter.transports[self.id] = (self.config, SimpleNamespace(auth_revoked=False, disconnect=AsyncMock()))
        self.adapter.transports[self.id][1].send_outbound = AsyncMock(return_value={"success": True, "start_status": "granted"})

    async def asyncTearDown(self):
        await self.adapter.disconnect()

    async def test_proxy_mode_rejected_at_startup_before_inbox_or_network(self):
        modules = {
            "senler_adapter_test": self.plugin,
            "hermes_cli.config": SimpleNamespace(load_config_readonly=lambda: {"gateway": {"proxy_url": "https://private.example.test"}}),
            "agent.secret_scope": SimpleNamespace(UnscopedSecretError=RuntimeError, get_secret=lambda name: None),
        }
        with patch.dict(sys.modules, modules), patch.object(self.relay, "RelayStore") as store, \
                patch.object(self.relay, "WebSocketRelayTransport") as transport:
            with self.assertRaisesRegex(ValueError, "same Hermes instance"):
                await self.adapter.connect()
        store.assert_not_called()
        transport.assert_not_called()
        self.assertFalse(self.adapter.tasks)

    def start_worker(self):
        self.adapter._running = True
        task = asyncio.create_task(self.adapter._work())
        self.adapter.tasks.append(task)
        return task

    async def wait_for_mode_block(self):
        async def wait():
            while not self.adapter.processing_blocked_reason:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), 1)

    async def test_live_proxy_blocks_starts_but_delivers_reports_and_resumes_waiting_event(self):
        finished = await self.queue_event("finished")
        self.complete_stored_event(finished, "Already completed")
        waiting = await self.queue_event("waiting")
        saved = self.store.subscriptions()
        private_url = "https://private-hermes.example.test"
        self.gateway_config["gateway"] = {"proxy_url": private_url}
        sender = self.adapter.transports[self.id][1].send_outbound
        with self.assertLogs(self.relay.log, level="INFO") as logs:
            task = self.start_worker()
            await self.wait_for_mode_block()
            sender.assert_not_awaited()
            self.assertIsNone(self.adapter.current)
            await self.adapter._report_results()
            self.assertEqual(sender.await_args.args[0]["op"], "senler_result")
            self.assertEqual(self.store.pending_results(self.id), [])
            await asyncio.sleep(1.05)  # A second blocked poll must not repeat the warning.
            self.assertEqual(len(logs.output), 1)
            self.assertEqual(self.store.subscriptions(), saved)
            self.assertEqual(self.store.next_event(saved)["attempt_id"], waiting["attempt_id"])
            self.gateway_config.clear()
            await asyncio.wait_for(task, 2)
        self.assertEqual([call.args[0]["op"] for call in sender.await_args_list], ["senler_result", "senler_start"])
        self.assertEqual(sender.await_args.args[0]["attempt_id"], waiting["attempt_id"])
        self.assertEqual(self.store.pending_results(self.id)[0]["failed"], 0)
        self.assertEqual(self.store.subscriptions(), saved)
        self.assertIsNone(self.adapter.processing_blocked_reason)
        self.assertIn("compatible again", logs.output[-1])
        self.assertNotIn(private_url, " ".join(logs.output))

    async def test_mode_change_during_start_grant_preserves_unexecuted_attempt(self):
        waiting = await self.queue_event("grant-in-flight")
        sender = self.adapter.transports[self.id][1].send_outbound

        async def grant(action):
            self.gateway_config["gateway"] = {"proxy_url": "https://private.example.test"}
            return {"success": True, "start_status": "granted"}

        sender.side_effect = grant
        task = self.start_worker()
        await self.wait_for_mode_block()
        self.assertFalse(hasattr(self.adapter, "seen"))
        self.assertIsNone(self.adapter.current)
        self.store.recover()
        self.assertEqual(self.store.next_event(self.store.subscriptions())["attempt_id"], waiting["attempt_id"])
        self.assertEqual(self.store.pending_results(self.id), [])
        self.gateway_config.clear()
        sender.side_effect = None
        await asyncio.wait_for(task, 2)
        self.assertEqual(sender.await_count, 2)
        self.assertEqual(sender.await_args_list[0], sender.await_args_list[1])
        self.assertEqual(self.adapter.seen.message_id, waiting["event_id"])
        self.assertEqual(self.store.pending_results(self.id)[0]["failed"], 0)

    async def test_fixing_mode_keeps_manual_pause_and_expires_waiting_events(self):
        waiting = await self.queue_event("paused")
        await self.queue_event("still-waiting")
        self.gateway_config["gateway"] = {"proxy_url": "https://private.example.test"}
        task = self.start_worker()
        await self.wait_for_mode_block()
        self.store.pause(self.id)
        saved = self.store.subscriptions()
        with self.store.db() as db:
            db.execute("UPDATE inbox SET expires=0 WHERE event_id=?", (waiting["event_id"],))
        # Expiry must proceed even while the gateway mode is still incompatible.
        async def expired():
            while not self.store.pending_results(self.id):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(expired(), 2)
        result = self.store.pending_results(self.id)[0]
        self.assertEqual(result["failed"], 1)
        self.assertIn("expired before processing", result["result"])
        self.gateway_config.clear()
        await asyncio.sleep(1.05)
        self.assertIsNone(self.adapter.processing_blocked_reason)
        self.assertEqual(self.store.subscriptions(), saved)
        self.assertFalse(self.store.subscriptions()[self.id]["enabled"])
        self.assertEqual(self.store.status()[0]["pending"], 1)
        self.adapter.transports[self.id][1].send_outbound.assert_not_awaited()
        self.assertFalse(hasattr(self.adapter, "seen"))
        self.assertFalse(task.done())

    async def test_incompatible_mode_rejects_direct_gateway_intake_with_active_attempt(self):
        row = await self.start_attempt("direct")
        event = SimpleNamespace(message_id=row["event_id"], source=SimpleNamespace(chat_id=self.id + ":direct"))
        self.adapter.current = event
        self.gateway_config["gateway"] = {"proxy_url": "https://private.example.test"}
        with self.actions.processing_attempt(self.adapter, row):
            await self.adapter.handle_message(event)
        self.assertFalse(event._gateway_accepted)
        self.assertFalse(hasattr(self.adapter, "seen"))

    async def test_mode_change_after_local_execution_boundary_does_not_replay_the_attempt(self):
        first = await self.queue_event("execution-boundary")
        await self.queue_event("next-event")
        start_processing = self.store.start_processing

        def start_then_change_mode(subscription, event_id, *args):
            start_processing(subscription, event_id, *args)
            if event_id == first["event_id"]:
                self.gateway_config["gateway"] = {"proxy_url": "https://private.example.test"}

        with patch.object(self.store, "start_processing", side_effect=start_then_change_mode):
            task = self.start_worker()
            await self.wait_for_mode_block()
            result = self.store.pending_results(self.id)[0]
            self.assertEqual(result["event_id"], first["event_id"])
            self.assertEqual(result["failed"], 1)
            self.assertIn("same Hermes instance", result["result"])
            self.assertFalse(hasattr(self.adapter, "seen"))
            self.gateway_config.clear()
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.adapter.seen.message_id, "next-event")
        sender = self.adapter.transports[self.id][1].send_outbound
        self.assertEqual([call.args[0]["event_id"] for call in sender.await_args_list], [first["event_id"], "next-event"])
        self.store.recover()
        self.assertIsNone(self.store.next_event(self.store.subscriptions()))
        self.assertEqual(self.store.pending_results(self.id)[0], result)

    def event(self, kind, **fields):
        return SimpleNamespace(text=json.dumps({"kind": kind, "webhook_id": self.id, "project_id": self.project, "revision": 0,
            "events": ["message_new"], "expires_at": "2099-01-01T00:00:00Z", **fields}))

    async def run_reported_event(self, completion, *, output="Operator report", outcome=Outcome.SUCCESS, event_id="reported"):
        await self.adapter._receive(self.config, self.event("control", active=True))
        await self.adapter._receive(self.config, self.event("event", payload={"event_id": event_id,
            "project_id": self.project, "event_type": "message_new", "data": {}}))

        async def handle(event):
            event._gateway_accepted = True
            self.processing_context = copy_context()
            if completion is not None:
                await asyncio.to_thread(self.actions.on_session_end, platform="senler-io", **completion)
            self.assertEqual(self.store.pending_results(self.id), [], "The hook must not finish the event before report delivery")
            sent = await self.adapter.send(event.source.chat_id, output)
            self.assertTrue(sent.success)  # Hermes also delivers model errors as text.
            await self.adapter.on_processing_complete(event, outcome)
            self.adapter._running = False

        self.adapter.handle_message = AsyncMock(side_effect=handle)
        self.adapter._running = True
        await asyncio.wait_for(self.adapter._work(), 2)
        result = self.store.pending_results(self.id)[0]
        self.assertEqual(result["event_id"], event_id)
        return result

    async def test_delivered_model_failure_interruption_and_incomplete_turn_are_failed(self):
        for index, (completion, reason) in enumerate([
            ({"completed": False, "failed": True, "interrupted": False}, "processing failed"),
            ({"completed": False, "failed": False, "interrupted": True}, "was interrupted"),
            ({"completed": False, "failed": False, "interrupted": False}, "did not complete"),
            ({"completed": True, "failed": True, "interrupted": False}, "processing failed"),
        ]):
            with self.subTest(completion=completion):
                row = await self.run_reported_event(completion, output="Action may already have completed", event_id=str(index))
                self.assertEqual(row["failed"], 1)
                self.assertIn(reason, row["result"])
                self.assertIn("Action may already have completed", row["result"])
                await self.adapter._report_results()

    async def test_missing_or_incomplete_completion_fields_do_not_imply_success(self):
        for index, completion in enumerate([
            None, {}, {"completed": True, "interrupted": False},
            {"completed": True, "failed": False, "interrupted": None},
            {"completed": 1, "failed": False, "interrupted": False},
            {"completed": True, "failed": "false", "interrupted": False},
        ]):
            with self.subTest(completion=completion):
                row = await self.run_reported_event(completion, output="Model request failed before finalization", event_id=str(index))
                self.assertEqual(row["failed"], 1)
                self.assertIn("completion was not confirmed", row["result"])
                self.assertIn("Model request failed before finalization", row["result"])
                await self.adapter._report_results()

    async def test_gateway_failure_or_cancellation_overrides_successful_model_completion(self):
        for outcome in [Outcome.FAILURE, Outcome.CANCELLED]:
            with self.subTest(outcome=outcome):
                row = await self.run_reported_event({"completed": True, "failed": False, "interrupted": False},
                                                    outcome=outcome, event_id=outcome.value)
                self.assertEqual(row["failed"], 1)
                self.assertIn("review prior actions", row["result"])
                self.assertIn("Operator report", row["result"])
                await self.adapter._report_results()

    async def test_successful_model_completion_waits_for_gateway_report(self):
        row = await self.run_reported_event({"completed": True, "failed": False, "interrupted": False},
                                           output="Nothing needed; no action taken")
        self.assertEqual(row["failed"], 0)
        self.assertEqual(row["result"], "Nothing needed; no action taken")

    async def test_accepted_report_survives_pause_during_gateway_processing(self):
        await self.adapter._receive(self.config, self.event("control", active=True))
        await self.adapter._receive(self.config, self.event("event", payload={"event_id": "paused-report",
            "project_id": self.project, "event_type": "message_new", "data": {}}))

        async def handle(event):
            event._gateway_accepted = True
            sent = await self.adapter.send(event.source.chat_id, "Checked the dialog. ")
            self.assertTrue(sent.success)
            reopened = type(self.store)(self.temp.name)
            with reopened.db() as db:
                saved = db.execute("SELECT state,result FROM inbox WHERE event_id=?", (event.message_id,)).fetchone()
            self.assertEqual(saved["state"], "processing")
            self.assertEqual(saved["result"], "Checked the dialog. ")
            self.assertEqual(reopened.pending_results(self.id), [])
            self.assertTrue((await self.adapter.send(event.source.chat_id, "Updated the lead.")).success)
            reopened.pause(self.id)
            self.assertFalse((await self.adapter.send(event.source.chat_id, "Late success")).success)
            self.actions.on_session_end(platform="senler-io", completed=True, failed=False, interrupted=False)
            await self.adapter.on_processing_complete(event, Outcome.SUCCESS)
            self.adapter._running = False

        self.adapter.handle_message = AsyncMock(side_effect=handle)
        self.adapter._running = True
        await asyncio.wait_for(self.adapter._work(), 2)
        result = self.store.pending_results(self.id)[0]
        self.assertEqual(result["failed"], 1)
        self.assertTrue(result["result"].startswith("Processing paused by the operator"))
        self.assertIn("Partial Hermes report:\nChecked the dialog. Updated the lead.", result["result"])
        self.assertNotIn("Late success", result["result"])
        self.store.recover()
        self.assertIsNone(self.store.next_event(self.store.subscriptions()))
        self.adapter.handle_message.assert_awaited_once()

    async def test_report_storage_failure_is_not_acknowledged(self):
        row = await self.start_attempt()
        chat_id = self.id + ":" + row["event_id"]
        self.adapter.current = SimpleNamespace(source=SimpleNamespace(chat_id=chat_id))
        with self.actions.processing_attempt(self.adapter, row):
            with patch.object(self.store, "append_report", side_effect=sqlite3.OperationalError("database is full")):
                sent = await self.adapter.send(chat_id, "Unsaved text")
            self.assertFalse(sent.success)
            self.assertIn("could not save", sent.error)
            self.assertTrue((await self.adapter.send(chat_id, "Saved retry")).success)
        reopened = type(self.store)(self.temp.name)
        reopened.recover()
        result = reopened.pending_results(self.id)[0]
        self.assertEqual(result["failed"], 1)
        self.assertIn("Partial Hermes report:\nSaved retry", result["result"])
        self.assertNotIn("Unsaved text", result["result"])

    async def test_old_report_cannot_enter_a_recreated_event_with_the_same_chat_id(self):
        old = await self.start_attempt()
        chat_id = self.id + ":" + old["event_id"]
        self.adapter.current = SimpleNamespace(source=SimpleNamespace(chat_id=chat_id))
        with self.actions.processing_attempt(self.adapter, old):
            old_context = copy_context()
        self.store.remove(self.id)
        connection = {key: value for key, value in self.config.items()
                      if key not in {"instructions", "enabled", "local_revision"}}
        self.store.configure(connection, self.config["instructions"])
        new = await self.start_attempt()
        self.assertNotEqual(old["attempt_id"], new["attempt_id"])
        self.assertFalse((await self.adapter.send(chat_id, "Unbound report")).success)
        with self.actions.processing_attempt(self.adapter, new):
            late = old_context.run(asyncio.create_task, self.adapter.send(chat_id, "Old report"))
            self.assertFalse((await late).success)
            self.assertTrue((await self.adapter.send(chat_id, "New report")).success)
        self.store.finish(self.id, new["event_id"], new["attempt_id"])
        self.assertEqual(self.store.pending_results(self.id)[0]["result"], "New report")

    async def test_old_completion_cannot_confirm_the_next_event(self):
        old = await self.run_reported_event(None, event_id="old")
        old_context = self.processing_context
        await self.adapter._report_results()
        observe = self.actions.on_session_end
        with patch.object(self.actions, "on_session_end", side_effect=lambda **kwargs: old_context.run(observe, **kwargs)):
            new = await self.run_reported_event({"completed": True, "failed": False, "interrupted": False}, event_id="new")
        self.assertEqual(new["failed"], 1)
        self.assertIn("completion was not confirmed", new["result"])
        with self.store.db() as db:
            saved = db.execute("SELECT failed,result FROM inbox WHERE event_id='old'").fetchone()
        self.assertEqual(saved["failed"], 1)
        self.assertEqual(saved["result"], old["result"])

    async def test_completion_cannot_rewrite_a_sealed_result_or_use_another_platform(self):
        row = await self.start_attempt()
        with self.actions.processing_attempt(self.adapter, row) as attempt:
            self.actions.on_session_end(platform="cli", completed=True, failed=False, interrupted=False)
            error = attempt.completion_error()
            self.assertIn("not confirmed", error)
            await asyncio.to_thread(self.actions.on_session_end, platform="senler-io", completed=True, failed=False, interrupted=False)
            self.assertEqual(attempt.completion_error(), error)

    async def test_failure_reason_survives_report_truncation(self):
        row = await self.run_reported_event({"completed": False, "failed": True, "interrupted": False}, output="x" * 16000)
        self.assertEqual(len(row["result"]), 16000)
        self.assertTrue(row["result"].startswith("Hermes processing failed"))
        self.assertTrue(row["result"].endswith("x" * 100))

    async def test_failed_result_retry_only_resends_saved_report(self):
        row = await self.run_reported_event({"completed": False, "failed": True, "interrupted": False})
        sender = self.adapter.transports[self.id][1].send_outbound
        sender.reset_mock()
        sender.side_effect = [asyncio.TimeoutError(), {"success": True}]
        await self.adapter._report_results()
        self.store.recover()
        self.assertIsNone(self.store.next_event(self.store.subscriptions()))
        await self.adapter._report_results()
        expected = {"op": "senler_result", "event_id": row["event_id"], "attempt_id": row["attempt_id"],
                    "result": row["result"], "failed": True}
        self.assertEqual([call.args[0] for call in sender.await_args_list], [expected, expected])
        self.assertEqual(self.store.pending_results(self.id), [])
        self.adapter.handle_message.assert_awaited_once()

    async def test_probe_never_invokes_gateway_and_pause_prevents_ack(self):
        await self.adapter._receive(self.config, self.event("probe", events=["message_new"], event_type="message_new"))
        self.assertIsNone(self.adapter.current)
        self.assertEqual(self.store.status()[0]["pending"], 0)
        self.store.pause(self.id)
        with self.assertRaisesRegex(ValueError, "paused"):
            await self.adapter._receive(self.config, self.event("probe", events=["message_new"], event_type="message_new"))

    async def test_durable_event_reaches_gateway_without_command_privileges_and_reports_result(self):
        await self.adapter._receive(self.config, self.event("control", active=True))
        await self.adapter._receive(self.config, self.event("event", payload={"event_id": "event-1", "project_id": self.project,
            "event_type": "message_new", "data": {"content": "/reset"}}))
        self.assertEqual(self.store.status()[0]["pending"], 1)
        self.adapter._running = True
        await asyncio.wait_for(self.adapter._work(), 2)
        self.assertFalse(self.adapter.seen.allow_gateway_control)
        self.assertIn(self.project, self.adapter.seen.text)
        self.assertIn("senler:" + self.id + ":event-1:reply", self.adapter.seen.text)
        self.assertEqual(self.adapter.toolsets_for_source(self.adapter.seen.source), ["mcp-senler-io-user"])
        self.assertEqual(self.store.pending_results(self.id)[0]["result"], "Prepared draft")
        self.assertEqual(self.store.pending_results(self.id)[0]["failed"], 0)

    async def test_native_control_updates_event_selection_without_rebinding(self):
        await self.adapter._receive(self.config, self.event("control", events=["lead_created"], revision=1, active=False))
        await self.adapter._receive(self.config, self.event("probe", events=["lead_created"], revision=1, event_type="lead_created"))
        await self.adapter._receive(self.config, self.event("control", events=["lead_created"], revision=1, active=True))
        self.assertEqual(self.adapter.allowed_subscriptions()[self.id]["events"], ["lead_created"])
        self.assertEqual(self.store.subscriptions()[self.id]["instructions"], "Prepare a draft only")

    async def test_server_refusal_does_not_invoke_the_model(self):
        self.adapter.transports[self.id][1].send_outbound.return_value = {"success": False, "start_status": "conflict"}
        await self.adapter._receive(self.config, self.event("control", active=True))
        await self.adapter._receive(self.config, self.event("event", payload={"event_id": "event-1", "project_id": self.project,
            "event_type": "message_new", "data": {}}))
        self.adapter._running = True
        task = asyncio.create_task(self.adapter._work())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(hasattr(self.adapter, "seen"))
        self.assertEqual(self.store.status()[0]["pending"], 0)
        self.assertEqual(self.store.pending_results(self.id), [], "A refusal must not overwrite the other attempt's result")

    async def test_no_report_can_be_sent_as_a_customer_message_outside_processing(self):
        result = await self.adapter.send("another-chat", "Unexpected send")
        self.assertFalse(result.success)

    async def test_unstarted_event_retries_the_same_attempt_after_transport_or_replay_failure(self):
        failures = [
            {"success": False, "error": "relay transport not connected"},
            {"success": False, "ambiguous": True, "error": "relay outbound timed out"},
            {"success": False, "start_status": "waiting"},
            asyncio.TimeoutError(),
        ]
        for index, failure in enumerate(failures):
            with self.subTest(failure=failure):
                await self.adapter._receive(self.config, self.event("control", active=True))
                await self.adapter._receive(self.config, self.event("event", payload={"event_id": "retry-" + str(index),
                    "project_id": self.project, "event_type": "message_new", "data": {}}))
                calls = []

                async def start(action):
                    calls.append(action)
                    self.assertEqual(self.store.status()[0]["pending"], 1)
                    if len(calls) == 1:
                        if isinstance(failure, Exception):
                            raise failure
                        return failure
                    self.assertEqual(calls[0]["attempt_id"], action["attempt_id"])
                    return {"success": True, "start_status": "granted"}

                self.adapter.transports[self.id][1].send_outbound.side_effect = start
                self.adapter._running = True
                with patch.object(self.relay, "START_RETRY_SECONDS", 0):
                    await asyncio.wait_for(self.adapter._work(), 2)
                self.assertEqual(len(calls), 2)
                self.assertEqual(self.store.status()[0]["pending"], 0)

    async def test_shutdown_before_start_keeps_waiting_event_after_restart(self):
        await self.adapter._receive(self.config, self.event("control", active=True))
        await self.adapter._receive(self.config, self.event("event", payload={"event_id": "pending", "project_id": self.project,
            "event_type": "message_new", "data": {}}))
        requesting = asyncio.Event()

        async def disconnected(action):
            requesting.set()
            await asyncio.Future()

        self.adapter.transports[self.id][1].send_outbound.side_effect = disconnected
        self.adapter._running = True
        task = asyncio.create_task(self.adapter._work())
        await asyncio.wait_for(requesting.wait(), 1)
        attempt = self.store.next_event(self.store.subscriptions())["attempt_id"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.store.recover()
        self.assertEqual(self.store.next_event(self.store.subscriptions())["attempt_id"], attempt)
        self.assertEqual(self.store.pending_results(self.id), [])
        self.assertFalse(hasattr(self.adapter, "seen"))

    async def test_shutdown_after_processing_begins_does_not_repeat_actions(self):
        await self.adapter._receive(self.config, self.event("control", active=True))
        await self.adapter._receive(self.config, self.event("event", payload={"event_id": "running", "project_id": self.project,
            "event_type": "message_new", "data": {}}))
        running = asyncio.Event()

        async def process(event):
            event._gateway_accepted = True
            await self.adapter.send(event.source.chat_id, "An action was attempted before interruption")
            running.set()
            await asyncio.Future()

        self.adapter.handle_message = AsyncMock(side_effect=process)
        self.adapter._running = True
        task = asyncio.create_task(self.adapter._work())
        await asyncio.wait_for(running.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.store.recover()
        self.assertIsNone(self.store.next_event(self.store.subscriptions()))
        self.assertEqual(self.store.pending_results(self.id)[0]["failed"], 1)
        self.assertIn("An action was attempted before interruption", self.store.pending_results(self.id)[0]["result"])
        self.adapter.handle_message.assert_awaited_once()

    def complete_stored_event(self, row, text):
        subscription = row["subscription"]
        self.store.start_processing(subscription, row["event_id"], row["attempt_id"],
                                    self.store.instructions(subscription)["local_revision"])
        self.store.append_report(subscription, row["event_id"], row["attempt_id"], text)
        self.store.finish(subscription, row["event_id"], row["attempt_id"])

    def add_reporting_subscription(self):
        other = "a" * 24
        connection = {key: value for key, value in self.config.items()
                      if key not in {"instructions", "enabled", "local_revision"}}
        connection.update(webhook_id=other, gateway_id=other, relay_url=connection["relay_url"].replace(self.id, other))
        self.store.configure(connection, "Report only")
        config = self.store.subscriptions()[other]
        self.store.accept(config, {"kind": "event", "webhook_id": other, "project_id": self.project, "revision": 0,
            "expires_at": "2099-01-01T00:00:00Z", "payload": {"event_id": "other-result", "project_id": self.project,
            "event_type": "message_new", "data": {}}})
        row = self.store.next_event({other: config})
        self.complete_stored_event(row, "Completed")
        sender = AsyncMock(return_value={"success": True})
        self.adapter.transports[other] = (config, SimpleNamespace(send_outbound=sender, auth_revoked=False, disconnect=AsyncMock()))
        return other, sender

    async def test_eight_paused_reports_do_not_block_another_subscription(self):
        for index in range(8):
            event_id = "paused-" + str(index)
            await self.adapter._receive(self.config, self.event("event", payload={"event_id": event_id,
                "project_id": self.project, "event_type": "message_new", "data": {}}))
            row = self.store.next_event(self.store.subscriptions())
            self.complete_stored_event(row, "Completed")
        self.store.pause(self.id)
        self.adapter.transports.pop(self.id)
        other, sender = self.add_reporting_subscription()
        await self.adapter._report_results()
        sender.assert_awaited_once()
        self.assertEqual(self.store.pending_results(other), [])
        self.assertEqual(len(self.store.pending_results(self.id)), 1)

    async def test_report_timeout_rotates_connections_and_resends_only_saved_result(self):
        await self.adapter._receive(self.config, self.event("event", payload={"event_id": "result-1",
            "project_id": self.project, "event_type": "message_new", "data": {}}))
        row = self.store.next_event(self.store.subscriptions())
        self.complete_stored_event(row, "Already completed")
        slow = self.adapter.transports[self.id][1].send_outbound
        async def timeout(action):
            await asyncio.Future()

        slow.side_effect = timeout
        other, sender = self.add_reporting_subscription()
        self.adapter.handle_message = AsyncMock()
        with patch.object(self.relay, "REPORT_BUDGET_SECONDS", 0.02):
            await self.adapter._report_results()
            sender.assert_not_awaited()
            await self.adapter._report_results()
        sender.assert_awaited_once()
        self.assertEqual(self.store.pending_results(other), [])
        slow.side_effect = None
        slow.return_value = {"success": True}
        await self.adapter._report_results()
        self.assertEqual(self.store.pending_results(self.id), [])
        self.assertEqual(slow.await_args.args[0]["result"], "Already completed")
        self.adapter.handle_message.assert_not_awaited()

    async def test_rejected_report_does_not_hide_newer_results_on_the_same_connection(self):
        for event_id in ["old", "new"]:
            await self.adapter._receive(self.config, self.event("event", payload={"event_id": event_id,
                "project_id": self.project, "event_type": "message_new", "data": {}}))
            row = self.store.next_event(self.store.subscriptions())
            self.complete_stored_event(row, "Completed")
        sender = self.adapter.transports[self.id][1].send_outbound
        sender.side_effect = [{"success": False}, {"success": True}, {"success": True}]
        await self.adapter._report_results()
        await self.adapter._report_results()
        self.assertEqual([call.args[0]["event_id"] for call in sender.await_args_list], ["old", "new"])
        self.assertEqual(self.store.pending_results(self.id)[0]["event_id"], "old")
        await self.adapter._report_results()
        self.assertEqual(sender.await_args.args[0]["event_id"], "old")
        self.assertEqual(self.store.pending_results(self.id), [])

    async def queue_event(self, event_id="event-1"):
        await self.adapter._receive(self.config, self.event("control", active=True))
        await self.adapter._receive(self.config, self.event("event", payload={"event_id": event_id, "project_id": self.project,
            "event_type": "message_new", "dialog_id": "dialog-1", "data": {}}))
        return self.store.next_event(self.store.subscriptions())

    async def start_attempt(self, event_id="event-1"):
        row = await self.queue_event(event_id)
        self.store.start_processing(self.id, row["event_id"], row["attempt_id"], self.store.instructions(self.id)["local_revision"])
        return row

    async def test_reply_hook_scopes_background_execution_and_blocks_after_pause(self):
        row = await self.start_attempt()
        args = {"method_name": "DialogsMessagingController_sendMessage", "parameters": {"id": "dialog-1", "content": "Hello"}}
        tool = "mcp__senler_io_user__execute"
        self.assertIsNone(self.actions.before_tool(tool, args, session_id="interactive"))
        with self.actions.processing_attempt(self.adapter, row):
            late = copy_context()
            for method in ["DialogsMessaging_sendMessage", "DialogsMessagingController_sendMessage"]:
                for direct in [False, True]:
                    with self.subTest(method=method, direct=direct):
                        args["method_name"] = method
                        name = "mcp__senler_io_user__" + method if direct else tool
                        params = args["parameters"] if direct else args
                        modified = self.actions.before_tool(name, params, session_id="background")
                        self.assertEqual(modified["action"], "modify")
                        prepared = modified["args"] if direct else modified["args"]["parameters"]
                        self.assertEqual(prepared["idempotency_key"], "senler:" + self.id + ":event-1:reply")
            changed = {**args, "parameters": {"id": "dialog-1", "content": "Changed answer"}}
            self.assertEqual(self.actions.before_tool(tool, changed, session_id="background")["action"], "block")
            self.store.pause(self.id)
            self.assertEqual(self.actions.before_tool(tool, args, session_id="background")["action"], "block")
        self.assertEqual(late.run(self.actions.before_tool, tool, args, session_id="background")["action"], "block")
        self.assertIsNone(self.actions.before_tool(tool, args, session_id="interactive"))

    async def test_late_worker_cannot_borrow_the_next_events_permission(self):
        old = await self.start_attempt("old")
        entered, release = threading.Event(), threading.Event()
        tool = "mcp__senler_io_user__DialogsMessaging_sendMessage"
        args = {"id": "dialog-1", "content": "Reply to old event"}

        def late_call():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("The test did not release the delayed worker")
            return self.actions.before_tool(tool, args, session_id="old-session")

        with self.actions.processing_attempt(self.adapter, old):
            worker = asyncio.create_task(asyncio.to_thread(late_call))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            self.store.finish(self.id, old["event_id"], old["attempt_id"], "Cancelled")
            new = await self.start_attempt("new")
            with self.actions.processing_attempt(self.adapter, new):
                release.set()
                self.assertEqual((await asyncio.wait_for(worker, 2))["action"], "block")
                allowed = self.actions.before_tool(tool, {**args, "content": "Reply to new event"}, session_id="new-session")
                self.assertEqual(allowed["args"]["idempotency_key"], "senler:" + self.id + ":new:reply")
        finally:
            release.set()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_pause_resume_and_restored_rules_block_all_old_mcp_calls(self):
        for change in ["pause", "rules"]:
            with self.subTest(change=change):
                row = await self.start_attempt(change)
                with self.actions.processing_attempt(self.adapter, row) as attempt:
                    revision = self.store.instructions(self.id)["local_revision"]
                    if change == "pause":
                        self.store.pause(self.id)
                        self.store.resume(self.id, revision + 1)
                    else:
                        self.store.update_instructions(self.id, "New rules", revision)
                        self.store.update_instructions(self.id, self.config["instructions"], revision + 1)
                    # Reads and other writes must also stop, not just sendMessage.
                    for method in ["search", "execute", "DialogsMessaging_sendMessage"]:
                        self.assertEqual(self.actions.before_tool("mcp__senler_io_user__" + method, {})["action"], "block")
                    with self.assertRaisesRegex(ValueError, "no longer active"):
                        await self.adapter.wait_for_processing(attempt)

    async def test_same_settings_remain_allowed_and_expired_processing_is_blocked(self):
        row = await self.start_attempt()
        with self.actions.processing_attempt(self.adapter, row) as attempt:
            self.store.update_instructions(self.id, self.config["instructions"], 0)
            connection = {k: v for k, v in self.config.items() if k not in {"instructions", "enabled", "local_revision"}}
            self.store.configure(connection, expected_local_revision=1)
            tool = "mcp__senler_io_user__search"
            self.assertIsNone(self.actions.before_tool(tool, {}))
            with patch.object(self.actions.time, "time", return_value=attempt.deadline + 1):
                self.assertEqual(self.actions.before_tool(tool, {})["action"], "block")

    async def test_worker_revokes_permission_before_waiting_for_gateway_cancellation(self):
        await self.adapter._receive(self.config, self.event("control", active=True))
        await self.adapter._receive(self.config, self.event("event", payload={"event_id": "stopping", "project_id": self.project,
            "event_type": "message_new", "data": {}}))
        contexts = []

        async def handle(event):
            event._gateway_accepted = True
            contexts.append(copy_context())

        async def cancel(session):
            result = contexts[0].run(self.actions.before_tool, "mcp__senler_io_user__search", {})
            self.assertEqual(result["action"], "block")
            self.adapter._running = False

        self.adapter.handle_message = AsyncMock(side_effect=handle)
        self.adapter.wait_for_processing = AsyncMock(side_effect=ValueError("Stop this attempt"))
        self.adapter.cancel_session_processing = AsyncMock(side_effect=cancel)
        self.adapter._running = True
        await asyncio.wait_for(self.adapter._work(), 2)
        self.adapter.cancel_session_processing.assert_awaited_once()

    async def test_gateway_recovery_cannot_bypass_the_inbox_attempt(self):
        row = await self.start_attempt()
        event = SimpleNamespace(message_id=row["event_id"], source=SimpleNamespace(
            chat_id=self.id + ":" + row["event_id"]))
        self.adapter.current = event
        with patch.object(FakeGatewayBase, "handle_message", new_callable=AsyncMock) as native:
            # Matching IDs or a still-processing SQLite row are not a launch grant.
            await self.adapter.handle_message(event)
            self.assertFalse(event._gateway_accepted)
            with self.actions.processing_attempt(self.adapter, row):
                context = copy_context()
                restored = SimpleNamespace(**vars(event))
                await self.adapter.handle_message(restored)
                self.assertFalse(restored._gateway_accepted)
                native.assert_not_awaited()
                await self.adapter.handle_message(event)
                native.assert_awaited_once_with(event)
            native.reset_mock()
            await context.run(asyncio.create_task, self.adapter.handle_message(event))
            native.assert_not_awaited()
            self.store.recover()
            await self.adapter.handle_message(event)
            native.assert_not_awaited()
        self.assertIsNone(self.actions.before_tool("ordinary_chat_tool", {}))

    async def test_database_read_failure_cannot_admit_a_gateway_turn(self):
        row = await self.start_attempt()
        self.adapter.current = SimpleNamespace(message_id=row["event_id"], source=SimpleNamespace(
            chat_id=self.id + ":" + row["event_id"]))
        with self.actions.processing_attempt(self.adapter, row):
            with patch.object(self.store, "require_processing", side_effect=sqlite3.OperationalError("unavailable")):
                with patch.object(FakeGatewayBase, "handle_message", new_callable=AsyncMock) as native:
                    await self.adapter.handle_message(self.adapter.current)
                    native.assert_not_awaited()
                    self.assertFalse(self.adapter.current._gateway_accepted)

    async def test_failed_finalization_revokes_tools_and_retries_only_the_saved_outcome(self):
        for stopped in [False, True]:
            with self.subTest(stopped=stopped):
                event_id = "finalize-" + str(stopped)
                await self.adapter._receive(self.config, self.event("control", active=True))
                await self.adapter._receive(self.config, self.event("event", payload={"event_id": event_id,
                    "project_id": self.project, "event_type": "message_new", "data": {}}))
                await self.adapter._receive(self.config, self.event("event", payload={"event_id": event_id + "-next",
                    "project_id": self.project, "event_type": "message_new", "data": {}}))
                contexts = []

                async def handle(event):
                    event._gateway_accepted = True
                    contexts.append(copy_context())
                    # Even a completed observer does not keep an ended attempt open.
                    self.actions.on_session_end(platform="senler-io", completed=True, failed=False, interrupted=False)
                    self.assertTrue((await self.adapter.send(event.source.chat_id, "Already performed")).success)
                    if stopped:
                        raise ValueError("Stop")
                    await self.adapter.on_processing_complete(event, Outcome.SUCCESS)

                finish = self.store.finish
                calls = []

                def fail_then_save(*args, **kwargs):
                    calls.append(args)
                    self.assertEqual(contexts[0].run(self.actions.before_tool,
                        "mcp__senler_io_user__search", {})["action"], "block")
                    with self.assertRaisesRegex(ValueError, "no longer active"):
                        contexts[0].run(self.actions.save_report, self.adapter, self.id + ":" + event_id, "Late report")
                    self.assertEqual(self.adapter.cancel_session_processing.await_count, int(stopped))
                    self.adapter.handle_message.assert_awaited_once()
                    if len(calls) < 3:
                        raise sqlite3.OperationalError("database is full")
                    finish(*args, **kwargs)
                    self.adapter._running = False

                self.adapter.handle_message = AsyncMock(side_effect=handle)
                self.adapter.cancel_session_processing = AsyncMock()
                self.adapter._running = True
                with patch.object(self.store, "finish", side_effect=fail_then_save):
                    with patch.object(self.relay, "FINALIZE_RETRY_SECONDS", 0):
                        await asyncio.wait_for(self.adapter._work(), 2)
                self.assertEqual(calls, [calls[0]] * 3, "Only the original outcome may be retried")
                result = self.store.pending_results(self.id)[0]
                self.assertEqual(result["failed"], int(stopped))
                self.assertIn("Already performed", result["result"])
                self.assertNotIn("Late report", result["result"])
                waiting = self.store.next_event(self.store.subscriptions())
                self.assertEqual(waiting["event_id"], event_id + "-next")
                self.store.finish(self.id, waiting["event_id"], waiting["attempt_id"], "Fixture complete", reported=True)
                await self.adapter._report_results()

    async def test_cancellation_during_tool_database_access_cannot_restore_permission(self):
        row = await self.start_attempt()
        tool = "mcp__senler_io_user__DialogsMessaging_sendMessage"
        for method in ["require_processing", "prepare_reply"]:
            with self.subTest(method=method):
                with self.actions.processing_attempt(self.adapter, row) as attempt:
                    original = getattr(self.store, method)
                    def close_during_database_call(*args):
                        result = original(*args)
                        attempt.close()
                        return result
                    with patch.object(self.store, method, side_effect=close_during_database_call):
                        result = await asyncio.to_thread(self.actions.before_tool, tool,
                            {"id": "dialog-1", "content": "Must not be sent"})
                    self.assertEqual(result["action"], "block")

    async def test_shutdown_cancels_gateway_even_when_result_cannot_be_saved(self):
        await self.adapter._receive(self.config, self.event("control", active=True))
        await self.adapter._receive(self.config, self.event("event", payload={"event_id": "shutdown-full-db",
            "project_id": self.project, "event_type": "message_new", "data": {}}))
        running = asyncio.Event()
        contexts = []

        async def handle(event):
            contexts.append(copy_context())
            running.set()
            await asyncio.Future()

        self.adapter.handle_message = AsyncMock(side_effect=handle)
        self.adapter.cancel_session_processing = AsyncMock()
        self.adapter._running = True
        with patch.object(self.store, "finish", side_effect=sqlite3.OperationalError("full")) as finish:
            task = asyncio.create_task(self.adapter._work())
            await asyncio.wait_for(running.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            self.adapter.cancel_session_processing.assert_awaited_once()
            finish.assert_called_once()
            self.assertEqual(contexts[0].run(self.actions.before_tool,
                "mcp__senler_io_user__search", {})["action"], "block")
        self.store.recover()
        self.assertEqual(self.store.pending_results(self.id)[0]["failed"], 1)
        self.assertIsNone(self.store.next_event(self.store.subscriptions()))

    async def maintain_once(self):
        self.adapter._running = True
        self.adapter._refresh_connections(self.store.subscriptions())
        await asyncio.gather(*(item[1] for item in self.adapter.connection_tasks.values()), return_exceptions=True)
        await self.adapter._report_results()

    def transport_fixture(self, *, idle=True, result=True):
        return SimpleNamespace(auth_revoked=False, connect=AsyncMock(), disconnect=AsyncMock(),
            handshake=AsyncMock(return_value=SimpleNamespace(contract_version=1, platform="senler-io", supports_op=lambda op: True)),
            go_idle=AsyncMock(return_value=idle), set_inbound_handler=Mock(),
            send_outbound=AsyncMock(return_value={"success": result}))

    async def paused_report(self):
        row = await self.start_attempt("paused-result")
        self.store.append_report(self.id, row["event_id"], row["attempt_id"], "Partial saved report")
        self.store.pause(self.id)
        old = self.adapter.transports[self.id][1]
        old.disconnect = AsyncMock()
        self.adapter.handle_message = AsyncMock()
        return row, old

    async def test_paused_reports_require_idle_ack_and_close_after_confirmation(self):
        row, old = await self.paused_report()
        transport = self.transport_fixture()

        async def send(action):
            transport.go_idle.assert_awaited_once()
            self.assertEqual(action["op"], "senler_result")
            self.assertEqual(action["attempt_id"], row["attempt_id"])
            self.assertIn("Partial saved report", action["result"])
            self.assertTrue(action["failed"])
            self.assertEqual(self.adapter.allowed_subscriptions(), {})
            return {"success": True}

        transport.send_outbound.side_effect = send
        with patch.object(self.relay, "WebSocketRelayTransport", return_value=transport) as factory:
            await self.maintain_once()
            self.assertFalse(factory.call_args.kwargs["reconnect"])
            await self.maintain_once()
            factory.assert_called_once()
        transport.set_inbound_handler.assert_not_called()
        old.disconnect.assert_awaited_once()
        transport.disconnect.assert_awaited_once()
        self.assertEqual(self.store.pending_results(self.id), [])
        self.assertFalse(self.store.subscriptions()[self.id]["enabled"])
        self.adapter.handle_message.assert_not_awaited()

    async def test_paused_report_retries_reenter_idle_and_do_not_restart_processing(self):
        await self.paused_report()
        no_ack = self.transport_fixture(idle=False)
        disconnected = self.transport_fixture(result=False)
        delivered = self.transport_fixture()
        with patch.object(self.relay, "WebSocketRelayTransport", side_effect=[no_ack, disconnected, delivered]):
            await self.maintain_once()
            no_ack.send_outbound.assert_not_awaited()
            no_ack.disconnect.assert_awaited_once()
            await self.maintain_once()
            self.assertTrue(self.store.pending_results(self.id))
            await self.maintain_once()
        for transport in [no_ack, disconnected, delivered]:
            transport.go_idle.assert_awaited_once()
            transport.set_inbound_handler.assert_not_called()
            transport.disconnect.assert_awaited_once()
        self.assertEqual(disconnected.send_outbound.await_args, delivered.send_outbound.await_args)
        self.assertEqual(self.store.pending_results(self.id), [])
        self.adapter.handle_message.assert_not_awaited()

    async def test_resume_replaces_idle_transport_before_admitting_events(self):
        await self.paused_report()
        idle = self.transport_fixture(result=False)
        active = self.transport_fixture()
        with patch.object(self.relay, "WebSocketRelayTransport", side_effect=[idle, active]) as factory:
            await self.maintain_once()
            self.store.resume(self.id, self.store.instructions(self.id)["local_revision"])
            self.adapter.controls[self.id] = (True, self.relay.time.monotonic())
            self.assertEqual(self.adapter.allowed_subscriptions(), {})
            await self.maintain_once()
            self.assertTrue(factory.call_args.kwargs["reconnect"])
        idle.disconnect.assert_awaited_once()
        active.go_idle.assert_not_awaited()
        active.set_inbound_handler.assert_called_once()
        self.assertEqual(self.adapter.allowed_subscriptions(), {}, "Fresh control/probes must precede processing")

    async def test_revoked_paused_connection_does_not_redial_with_the_same_key(self):
        await self.paused_report()
        transport = self.transport_fixture(result=False)
        async def revoked(action):
            transport.auth_revoked = True
            return {"success": False}
        transport.send_outbound.side_effect = revoked
        with patch.object(self.relay, "WebSocketRelayTransport", return_value=transport) as factory:
            await self.maintain_once()
            await self.maintain_once()
            factory.assert_called_once()
        self.assertEqual(self.adapter.allowed_subscriptions(), {})
        self.assertTrue(self.store.pending_results(self.id))

    async def test_slow_connection_does_not_hold_up_healthy_reports_or_duplicate_dials(self):
        await self.paused_report()
        entered = asyncio.Event()
        transport = self.transport_fixture()
        async def blocked():
            entered.set()
            await asyncio.Future()
        transport.connect.side_effect = blocked
        self.adapter._running = True
        with patch.object(self.relay, "WebSocketRelayTransport", return_value=transport) as factory:
            self.adapter._refresh_connections(self.store.subscriptions())
            await asyncio.wait_for(entered.wait(), 1)
            other, sender = self.add_reporting_subscription()
            reported = asyncio.Event()
            async def confirm(action):
                reported.set()
                return {"success": True}
            sender.side_effect = confirm
            self.adapter.tasks = [asyncio.create_task(self.adapter._maintain())]
            await asyncio.wait_for(reported.wait(), 1)
            self.assertFalse(self.adapter.connection_tasks[self.id][1].done())
            self.adapter._refresh_connections(self.store.subscriptions())
            factory.assert_called_once()
            await self.adapter.disconnect()
        self.assertEqual(self.store.pending_results(other), [])
        self.assertTrue(self.store.pending_results(self.id))
        transport.disconnect.assert_awaited_once()
        self.adapter.handle_message.assert_not_awaited()

    async def test_retiring_socket_cannot_send_paused_report_before_idle_confirmation(self):
        _, old = await self.paused_report()
        old.send_outbound.reset_mock()
        transport = self.transport_fixture(idle=False)
        self.adapter._running = True
        with patch.object(self.relay, "WebSocketRelayTransport", return_value=transport):
            self.adapter._refresh_connections(self.store.subscriptions())
            # The old socket is still owned until its replacement task starts,
            # but it must already be excluded from the normal reporting pass.
            await self.adapter._report_results()
            await asyncio.gather(*(item[1] for item in self.adapter.connection_tasks.values()))
        old.send_outbound.assert_not_awaited()
        transport.send_outbound.assert_not_awaited()
        self.assertTrue(self.store.pending_results(self.id))

    async def test_idle_request_timeout_closes_and_retries_saved_report(self):
        await self.paused_report()
        stalled, ready = self.transport_fixture(), self.transport_fixture()
        async def blocked(**kwargs):
            await asyncio.Future()
        stalled.go_idle.side_effect = blocked
        with patch.object(self.relay, "IDLE_TIMEOUT_SECONDS", 0.02), \
                patch.object(self.relay, "WebSocketRelayTransport", side_effect=[stalled, ready]):
            await asyncio.wait_for(self.maintain_once(), 1)
            stalled.disconnect.assert_awaited_once()
            stalled.send_outbound.assert_not_awaited()
            self.assertTrue(self.store.pending_results(self.id))
            await self.maintain_once()
        self.assertEqual(self.store.pending_results(self.id), [])
        self.adapter.handle_message.assert_not_awaited()

    async def test_connection_and_handshake_timeouts_close_and_retry(self):
        for phase in ["connect", "handshake"]:
            with self.subTest(phase=phase):
                self.adapter.transports.clear()
                stalled = self.transport_fixture()
                ready = self.transport_fixture()
                async def blocked():
                    await asyncio.Future()
                getattr(stalled, phase).side_effect = blocked
                with patch.object(self.relay, "CONNECT_TIMEOUT_SECONDS", 0.02), \
                        patch.object(self.relay, "WebSocketRelayTransport", side_effect=[stalled, ready]):
                    await asyncio.wait_for(self.maintain_once(), 1)
                    stalled.disconnect.assert_awaited_once()
                    self.assertNotIn(self.id, self.adapter.transports)
                    await asyncio.wait_for(self.maintain_once(), 1)
                    self.assertIs(self.adapter.transports[self.id][1], ready)
                await self.adapter.disconnect()

    async def test_connection_attempt_limit_admits_next_subscription_after_completion(self):
        other, _ = self.add_reporting_subscription()
        self.adapter.transports.clear()
        entered, release = asyncio.Event(), asyncio.Event()
        first, second = self.transport_fixture(), self.transport_fixture()
        async def connect():
            entered.set()
            await release.wait()
        first.connect.side_effect = connect
        self.adapter._running = True
        try:
            with patch.object(self.relay, "MAX_CONNECTION_TASKS", 1), \
                    patch.object(self.relay, "WebSocketRelayTransport", side_effect=[first, second]) as factory:
                self.adapter._refresh_connections(self.store.subscriptions())
                await asyncio.wait_for(entered.wait(), 1)
                self.adapter._refresh_connections(self.store.subscriptions())
                factory.assert_called_once()
                self.assertEqual(len(self.adapter.connection_tasks), 1)
                release.set()
                await asyncio.gather(*(item[1] for item in self.adapter.connection_tasks.values()))
                await self.maintain_once()
                self.assertIs(self.adapter.transports[other][1], second)
        finally:
            release.set()

    async def test_changed_key_waits_for_old_connection_cleanup_before_redial(self):
        self.adapter.transports.clear()
        entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        old, new = self.transport_fixture(), self.transport_fixture()
        async def blocked():
            entered.set()
            await asyncio.Future()
        async def cleanup(**kwargs):
            closing.set()
            await release.wait()
        old.handshake.side_effect = blocked
        old.disconnect.side_effect = cleanup
        self.adapter._running = True
        try:
            with patch.object(self.relay, "WebSocketRelayTransport", side_effect=[old, new]) as factory:
                self.adapter._refresh_connections(self.store.subscriptions())
                await asyncio.wait_for(entered.wait(), 1)
                stale_task = self.adapter.connection_tasks[self.id][1]
                connection = {key: value for key, value in self.config.items()
                              if key not in {"instructions", "enabled", "local_revision"}}
                connection["gateway_secret"] = "rotated-" + "x" * 40
                self.store.configure(connection, expected_local_revision=0)
                self.adapter._refresh_connections(self.store.subscriptions())
                await asyncio.wait_for(closing.wait(), 1)
                self.adapter._refresh_connections(self.store.subscriptions())
                self.assertFalse(stale_task.done(), "A second cancellation interrupted socket cleanup")
                factory.assert_called_once()
                release.set()
                await asyncio.gather(stale_task, return_exceptions=True)
                await self.maintain_once()
                self.assertIs(self.adapter.transports[self.id][1], new)
                self.assertEqual(factory.call_args.kwargs["upgrade_secret"], connection["gateway_secret"])
                self.assertNotIn(self.id, self.adapter.controls)
                self.assertEqual(self.adapter.allowed_subscriptions(), {})
        finally:
            release.set()

    async def test_pause_or_remove_during_handshake_cannot_publish_a_connection(self):
        for action in ["pause", "remove"]:
            with self.subTest(action=action):
                if self.id not in self.store.subscriptions():
                    connection = {key: value for key, value in self.config.items()
                                  if key not in {"instructions", "enabled", "local_revision"}}
                    self.store.configure(connection, "Report only")
                elif not self.store.subscriptions()[self.id]["enabled"]:
                    self.store.resume(self.id, self.store.instructions(self.id)["local_revision"])
                self.adapter.transports.clear()
                entered, release = asyncio.Event(), asyncio.Event()
                transport = self.transport_fixture()
                async def handshake():
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(contract_version=1, platform="senler-io", supports_op=lambda op: True)
                transport.handshake.side_effect = handshake
                self.adapter._running = True
                with patch.object(self.relay, "WebSocketRelayTransport", return_value=transport):
                    self.adapter._refresh_connections(self.store.subscriptions())
                    await asyncio.wait_for(entered.wait(), 1)
                    getattr(self.store, action)(self.id)
                    # Finish before the supervisor observes the change: the dial
                    # must itself recheck the saved settings before publishing.
                    release.set()
                    await asyncio.gather(*(item[1] for item in self.adapter.connection_tasks.values()))
                self.assertNotIn(self.id, self.adapter.transports)
                transport.disconnect.assert_awaited_once()
                await self.adapter.disconnect()

    async def test_shutdown_before_connection_task_starts_still_closes_old_socket(self):
        _, old = await self.paused_report()
        self.adapter._running = True
        with patch.object(self.relay, "WebSocketRelayTransport") as factory:
            self.adapter._refresh_connections(self.store.subscriptions())
            await self.adapter.disconnect()
        old.disconnect.assert_awaited_once()
        factory.assert_not_called()
        self.assertFalse(self.adapter.transports)
        self.assertFalse(self.adapter.connection_tasks)

    async def test_paused_report_retries_rotate_past_rejected_results(self):
        await self.paused_report()
        self.store.resume(self.id, self.store.instructions(self.id)["local_revision"])
        row = await self.start_attempt("new-result")
        self.store.finish(self.id, row["event_id"], row["attempt_id"])
        self.store.pause(self.id)
        rejected, accepted, retried = (self.transport_fixture(result=False), self.transport_fixture(), self.transport_fixture())
        with patch.object(self.relay, "WebSocketRelayTransport", side_effect=[rejected, accepted, retried]):
            await self.maintain_once()
            await self.maintain_once()
            self.assertEqual(accepted.send_outbound.await_args.args[0]["event_id"], "new-result")
            await self.maintain_once()
            self.assertEqual(retried.send_outbound.await_args.args[0]["event_id"], "paused-result")
        self.assertEqual(self.store.pending_results(self.id), [])


if __name__ == "__main__":
    unittest.main()
