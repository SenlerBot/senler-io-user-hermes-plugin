import importlib.util
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("senler_store_tests", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
store_module = importlib.import_module(spec.name + ".senler_relay_store")


class RelayStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = store_module.RelayStore(self.temp.name)
        self.id = "123456789012345678901234"
        self.connection = {"webhook_id": self.id, "project_id": str(uuid4()), "gateway_id": self.id,
            "relay_url": "wss://api.example.test/api/hermes-relay/" + self.id,
            "gateway_secret": "fixture-credential-" + "x" * 32, "events": ["message_new", "lead_created"], "revision": 0}
        self.store.configure(self.connection, "Prepare a draft")
        self.config = self.store.subscriptions()[self.id]

    def envelope(self, **overrides):
        return {"kind": "event", "webhook_id": self.id, "project_id": self.connection["project_id"], "revision": 0,
            "expires_at": "2099-01-01T00:00:00Z",
            "payload": {"event_id": "event-1", "dialog_id": "dialog-1", "event_type": "message_new", "project_id": self.connection["project_id"], "data": {"content": "/reset"}}, **overrides}

    def start_event(self):
        self.store.accept(self.config, self.envelope())
        row = self.store.next_event(self.store.subscriptions())
        self.store.start_processing(self.id, row["event_id"], row["attempt_id"], self.store.instructions(self.id)["local_revision"])
        return row

    def test_report_is_durable_before_completion_and_not_published_early(self):
        row = self.start_event()
        self.store.append_report(self.id, row["event_id"], row["attempt_id"], "First part. ")
        reopened = store_module.RelayStore(self.temp.name)
        reopened.append_report(self.id, row["event_id"], row["attempt_id"], "Second part.")
        with reopened.db() as db:
            saved = db.execute("SELECT state,result FROM inbox WHERE event_id=?", (row["event_id"],)).fetchone()
        self.assertEqual(saved["state"], "processing")
        self.assertEqual(saved["result"], "First part. Second part.")
        self.assertEqual(reopened.pending_results(self.id), [])
        reopened.finish(self.id, row["event_id"], row["attempt_id"])
        self.assertEqual(reopened.pending_results(self.id)[0]["result"], saved["result"])
        self.assertEqual(reopened.pending_results(self.id)[0]["failed"], 0)

    def test_all_stop_paths_preserve_partial_report_and_reject_late_text(self):
        changes = [
            ("pause", lambda: self.store.pause(self.id)),
            ("instructions", lambda: self.store.update_instructions(self.id, "New instructions", 0)),
            ("connection", lambda: self.store.configure({**self.connection, "gateway_secret": "new" * 20}, expected_local_revision=0)),
            ("selection", lambda: self.store.configure({**self.connection, "events": ["lead_created"], "revision": 1}, expected_local_revision=0)),
            ("server pause", lambda: self.store.sync_settings(self.id, self.envelope(kind="control", active=False, events=self.connection["events"]))),
            ("server selection", lambda: self.store.sync_settings(self.id, self.envelope(kind="control", active=True, events=["lead_created"], revision=1))),
            ("restart", lambda: store_module.RelayStore(self.temp.name).recover()),
        ]
        for label, stop in changes:
            with self.subTest(stop=label):
                self.store.remove(self.id)
                self.store.configure(self.connection, "Prepare a draft")
                row = self.start_event()
                self.store.append_report(self.id, row["event_id"], row["attempt_id"], "An action was attempted")
                stop()
                reopened = store_module.RelayStore(self.temp.name)
                result = reopened.pending_results(self.id)[0]
                self.assertEqual(result["failed"], 1)
                self.assertIn("Partial Hermes report:\nAn action was attempted", result["result"])
                with self.assertRaises(ValueError):
                    reopened.append_report(self.id, row["event_id"], row["attempt_id"], "Late text")
                reopened.finish(self.id, row["event_id"], row["attempt_id"])
                reopened.recover()
                self.assertEqual(reopened.pending_results(self.id)[0], result)
                self.assertIsNone(reopened.next_event(reopened.subscriptions()))

    def test_report_limit_preserves_stop_reason_and_never_bypasses_attempt_checks(self):
        row = self.start_event()
        self.store.append_report(self.id, row["event_id"], row["attempt_id"], "x" * (store_module.MAX_REPORT_CHARS + 100))
        self.store.append_report(self.id, row["event_id"], row["attempt_id"], "Overflow")
        with self.store.db() as db:
            report = db.execute("SELECT result FROM inbox WHERE event_id=?", (row["event_id"],)).fetchone()[0]
        self.assertEqual(report, "x" * store_module.MAX_REPORT_CHARS)
        self.store.pause(self.id)
        result = self.store.pending_results(self.id)[0]
        self.assertEqual(len(result["result"]), store_module.MAX_REPORT_CHARS)
        self.assertTrue(result["result"].startswith("Processing paused by the operator"))
        self.assertTrue(result["result"].endswith("x" * 100))
        with self.assertRaises(ValueError):
            self.store.append_report(self.id, row["event_id"], row["attempt_id"], "")

    def test_pause_waits_for_in_flight_report_commit(self):
        row = self.start_event()
        writing, pause_started = threading.Event(), threading.Event()
        original = self.store._processing_row

        def processing_row(*args):
            result = original(*args)
            writing.set()  # append_report already holds BEGIN IMMEDIATE.
            if not pause_started.wait(3):
                raise TimeoutError("Pause worker did not start")
            return result

        def pause():
            if not writing.wait(3):
                raise TimeoutError("Report worker did not start")
            pause_started.set()
            self.store.pause(self.id)

        with patch.object(self.store, "_processing_row", side_effect=processing_row), ThreadPoolExecutor(max_workers=2) as pool:
            report = pool.submit(self.store.append_report, self.id, row["event_id"], row["attempt_id"], "Committed before pause")
            stopped = pool.submit(pause)
            report.result(timeout=4)
            stopped.result(timeout=4)
        saved = self.store.pending_results(self.id)[0]
        self.assertEqual(saved["failed"], 1)
        self.assertIn("Committed before pause", saved["result"])

    def test_pending_and_expired_attempts_cannot_append_report(self):
        self.store.accept(self.config, self.envelope())
        row = self.store.next_event(self.store.subscriptions())
        with self.assertRaises(ValueError):
            self.store.append_report(self.id, row["event_id"], row["attempt_id"], "Not started")
        self.store.finish(self.id, row["event_id"], row["attempt_id"])
        self.assertEqual(self.store.pending_results(self.id), [])
        self.store.start_processing(self.id, row["event_id"], row["attempt_id"], 0)
        with patch.object(store_module.time, "time", return_value=row["expires"] + 1), self.assertRaises(ValueError):
            self.store.append_report(self.id, row["event_id"], row["attempt_id"], "Expired")

    def test_identity_is_stable_and_status_does_not_disclose_credentials(self):
        self.assertEqual(self.store.identity(), store_module.RelayStore(self.temp.name).identity())
        public = json.dumps(self.store.status())
        self.assertNotIn(self.connection["gateway_secret"], public)
        self.assertNotIn("instructions", public)

    def test_probes_and_control_never_enter_agent_inbox(self):
        for event_type in self.connection["events"]:
            self.assertEqual(self.store.accept(self.config, self.envelope(kind="probe", event_type=event_type, events=self.connection["events"])), "probe")
        self.store.accept(self.config, self.envelope(kind="control", active=True))
        self.assertIsNone(self.store.next_event({self.id: self.config}))
        with self.assertRaises(ValueError):
            self.store.accept(self.config, self.envelope(kind="probe", event_type="message_new", events=["message_new"]))

    def test_envelope_and_nested_payload_must_match_configured_project_and_events(self):
        invalid = [self.envelope(project_id=str(uuid4())), self.envelope(webhook_id="other"), self.envelope(revision=1),
            self.envelope(payload={**self.envelope()["payload"], "project_id": str(uuid4())}),
            self.envelope(payload={**self.envelope()["payload"], "event_type": "unexpected"})]
        for envelope in invalid:
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                self.store.accept(self.config, envelope)
        self.assertEqual(self.store.status()[0]["pending"], 0)

    def test_receipt_is_durable_and_duplicate_does_not_repeat_completed_work(self):
        self.assertEqual(self.store.accept(self.config, self.envelope()), "event")
        after_restart = store_module.RelayStore(self.temp.name)
        self.assertEqual(after_restart.accept(self.config, self.envelope()), "duplicate")
        row = after_restart.next_event({self.id: self.config})
        self.assertIn("/reset", row["payload"])
        after_restart.start_processing(self.id, row["event_id"], row["attempt_id"], self.config["local_revision"])
        after_restart.append_report(self.id, row["event_id"], row["attempt_id"], "Draft")
        after_restart.finish(self.id, row["event_id"], row["attempt_id"])
        self.assertEqual(after_restart.pending_results(self.id)[0]["result"], "Draft")
        after_restart.reported(self.id, row["event_id"], row["attempt_id"])
        self.assertEqual(after_restart.accept(self.config, self.envelope()), "duplicate")
        self.assertIsNone(after_restart.next_event({self.id: self.config}))
        self.assertEqual(after_restart.pending_results(self.id)[0]["result"], "Draft")

    def test_interrupted_work_is_reported_without_repeating_uncertain_actions(self):
        self.store.accept(self.config, self.envelope())
        first = self.store.next_event({self.id: self.config})
        self.store.start_processing(self.id, first["event_id"], first["attempt_id"], self.config["local_revision"])
        self.store.recover()
        self.assertIsNone(self.store.next_event({self.id: self.config}))
        result = self.store.pending_results(self.id)[0]
        self.assertEqual(result["event_id"], first["event_id"])
        self.assertEqual(result["failed"], 1)
        self.assertIn("interrupted", result["result"])

    def test_unstarted_attempt_survives_restart_and_reuses_its_identity(self):
        self.store.accept(self.config, self.envelope())
        first = self.store.next_event({self.id: self.config})
        reopened = store_module.RelayStore(self.temp.name)
        reopened.recover()
        replay = reopened.next_event(reopened.subscriptions())
        self.assertRegex(first["attempt_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(replay["attempt_id"], first["attempt_id"])
        self.assertEqual(reopened.pending_results(self.id), [])
        # Recreating an inbox is a new attempt, even for the same business event.
        reopened.remove(self.id)
        reopened.configure(self.connection, "Prepare a draft")
        reopened.accept(reopened.subscriptions()[self.id], self.envelope())
        self.assertNotEqual(reopened.next_event(reopened.subscriptions())["attempt_id"], first["attempt_id"])

    def test_existing_inbox_is_migrated_without_discarding_queued_events(self):
        self.store.accept(self.config, self.envelope())
        with self.store.db() as db:
            db.execute("ALTER TABLE inbox DROP COLUMN attempt_id")
        reopened = store_module.RelayStore(self.temp.name)
        row = reopened.next_event(reopened.subscriptions())
        self.assertRegex(row["attempt_id"], r"^[a-f0-9]{32}$")
        self.assertEqual(row["event_id"], "event-1")
        self.assertEqual(reopened.instructions(self.id)["instructions"], "Prepare a draft")

    def test_start_requires_current_policy_and_never_claims_the_same_event_twice(self):
        self.store.accept(self.config, self.envelope())
        row = self.store.next_event(self.store.subscriptions())
        self.store.update_instructions(self.id, "Only support requests", 0)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.start_processing(self.id, "event-1", row["attempt_id"], 0)
        self.assertEqual(self.store.status()[0]["pending"], 1)
        self.store.start_processing(self.id, "event-1", row["attempt_id"], 1)
        with self.assertRaisesRegex(ValueError, "no longer pending"):
            self.store.start_processing(self.id, "event-1", row["attempt_id"], 1)

    def test_instruction_edit_preserves_pause_credentials_and_rejects_lost_updates(self):
        self.store.pause(self.id)
        before = self.store.subscriptions()[self.id]
        public = self.store.instructions(self.id)
        self.assertEqual(public["instructions"], "Prepare a draft")
        self.assertNotIn("gateway_secret", public)
        self.assertNotIn(self.connection["gateway_secret"], json.dumps(public))
        self.store.update_instructions(self.id, "Prepare a draft; only support requests", public["local_revision"])
        after = self.store.subscriptions()[self.id]
        self.assertEqual(after, {**before, "instructions": "Prepare a draft; only support requests", "local_revision": 2})
        for revision in [None, True, public["local_revision"]]:
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "changed"):
                self.store.update_instructions(self.id, "Overwrite", revision)
        self.assertEqual(self.store.subscriptions()[self.id], after)

    def test_paused_or_disconnected_subscription_does_not_start_work(self):
        self.store.accept(self.config, self.envelope())
        self.assertIsNone(self.store.next_event({}))
        self.store.pause(self.id)
        self.assertFalse(self.store.subscriptions()[self.id]["enabled"])
        with self.assertRaisesRegex(ValueError, "changed"):
            self.store.configure(self.connection, "Old setup", expected_local_revision=0)
        self.assertFalse(self.store.subscriptions()[self.id]["enabled"])

    def test_waiting_event_uses_new_rules_after_reconfiguration(self):
        self.store.accept(self.config, self.envelope())
        self.store.configure({**self.connection, "revision": 1}, "New policy", expected_local_revision=0)
        row = self.store.next_event(self.store.subscriptions())
        self.assertEqual(row["revision"], 1)
        self.assertEqual(self.store.subscriptions()[self.id]["instructions"], "New policy")
        self.assertEqual(self.store.pending_results(self.id), [])

    def test_rebinding_preserves_operator_policy_and_requires_current_local_revision(self):
        self.store.configure(self.connection, "Only process support requests", 0)
        self.store.configure({**self.connection, "revision": 1}, expected_local_revision=1)
        updated = self.store.subscriptions()[self.id]
        self.assertEqual(updated["instructions"], "Only process support requests")
        self.assertNotIn("mode", updated)
        self.assertEqual(updated["revision"], 1)
        self.assertEqual(updated["local_revision"], 2)

    def test_resume_requires_current_revision_and_keeps_saved_instructions(self):
        self.store.pause(self.id)
        for stale_revision in [None, 0]:
            with self.subTest(revision=stale_revision), self.assertRaisesRegex(ValueError, "changed"):
                self.store.resume(self.id, stale_revision)
        self.assertFalse(self.store.subscriptions()[self.id]["enabled"])
        self.store.resume(self.id, 1)
        resumed = self.store.subscriptions()[self.id]
        self.assertTrue(resumed["enabled"])
        self.assertEqual(resumed["local_revision"], 2)
        self.assertEqual(resumed["instructions"], "Prepare a draft")
        self.assertNotIn("mode", resumed)

    def test_refresh_preserves_pause_and_server_sync_preserves_local_instructions(self):
        self.store.pause(self.id)
        self.store.configure(self.connection, expected_local_revision=1)
        self.store.sync_settings(self.id, self.envelope(kind="control", active=True, events=["lead_created"], revision=1))
        current = self.store.subscriptions()[self.id]
        self.assertFalse(current["enabled"])
        self.assertEqual(current["instructions"], "Prepare a draft")
        self.assertEqual(current["events"], ["lead_created"])
        self.assertEqual(current["local_revision"], 3)

    def test_event_selection_sync_keeps_matching_backlog_and_rejects_removed_types(self):
        self.store.accept(self.config, self.envelope())
        self.store.sync_settings(self.id, self.envelope(kind="control", events=["message_new"], revision=1))
        row = self.store.next_event(self.store.subscriptions())
        self.assertEqual(row["revision"], 1)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.sync_settings(self.id, self.envelope(kind="control", events=["message_new"]))

    def test_transport_retention_does_not_depend_on_business_event_age(self):
        event = self.envelope()
        event["payload"]["timestamp"] = "2000-01-01T00:00:00Z"
        self.store.accept(self.config, event)
        row = self.store.next_event(self.store.subscriptions())
        self.assertIsNotNone(row)
        self.store.start_processing(self.id, "event-1", row["attempt_id"], self.config["local_revision"])
        other = self.envelope(expires_at="2000-01-01T00:00:00Z")
        other["payload"]["event_id"] = "expired"
        self.store.accept(self.config, other)
        self.assertIsNone(self.store.next_event({}))
        self.assertIn("expired", self.store.pending_results(self.id)[0]["result"])

    def test_reply_identity_is_persisted_and_cannot_change_recipient_or_content(self):
        self.store.accept(self.config, self.envelope())
        row = self.store.next_event(self.store.subscriptions())
        self.store.start_processing(self.id, "event-1", row["attempt_id"], self.config["local_revision"])
        original = {"id": "dialog-1", "content": "Hello", "idempotency_key": "model-key"}
        prepared = self.store.prepare_reply(self.id, "event-1", row["attempt_id"], original)
        self.assertEqual(prepared["idempotency_key"], "senler:" + self.id + ":event-1:reply")
        reopened = store_module.RelayStore(self.temp.name)
        self.assertEqual(reopened.prepare_reply(self.id, "event-1", row["attempt_id"], original), prepared)
        for change in [{"id": "other"}, {"content": "Another reply"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                reopened.prepare_reply(self.id, "event-1", row["attempt_id"], {**original, **change})

    def test_pause_revokes_running_attempt_durably_and_preserves_waiting_events(self):
        self.store.accept(self.config, self.envelope())
        row = self.store.next_event(self.store.subscriptions())
        self.store.start_processing(self.id, row["event_id"], row["attempt_id"], 0)
        waiting = self.envelope()
        waiting["payload"]["event_id"] = "waiting"
        self.store.accept(self.config, waiting)
        self.store.pause(self.id)
        # A separate helper/gateway process sees the same committed cancellation.
        reopened = store_module.RelayStore(self.temp.name)
        reopened.resume(self.id, reopened.instructions(self.id)["local_revision"])
        with self.assertRaisesRegex(ValueError, "no longer active"):
            reopened.require_processing(self.id, row["event_id"], row["attempt_id"])
        report = reopened.pending_results(self.id)[0]
        self.assertEqual(report["failed"], 1)
        self.assertIn("paused", report["result"])
        reopened.finish(self.id, row["event_id"], row["attempt_id"])
        self.assertEqual(reopened.pending_results(self.id)[0], report)
        self.assertEqual(reopened.next_event(reopened.subscriptions())["event_id"], "waiting")

    def test_restoring_rules_or_credentials_never_revives_cancelled_attempt(self):
        for change in ["instructions", "configure", "credentials"]:
            with self.subTest(change=change):
                self.store.remove(self.id)
                self.store.configure(self.connection, "Prepare a draft")
                self.store.accept(self.config, self.envelope())
                row = self.store.next_event(self.store.subscriptions())
                self.store.start_processing(self.id, row["event_id"], row["attempt_id"], 0)
                if change == "instructions":
                    self.store.update_instructions(self.id, "New rules", 0)
                    self.store.update_instructions(self.id, "Prepare a draft", 1)
                else:
                    new_connection = {**self.connection, "gateway_secret": "new" * 20} if change == "credentials" else self.connection
                    self.store.configure(new_connection, "New rules" if change == "configure" else "Prepare a draft", 0)
                    self.store.configure(self.connection, "Prepare a draft", 1)
                with self.assertRaises(ValueError):
                    self.store.require_processing(self.id, row["event_id"], row["attempt_id"])
                self.assertEqual(self.store.pending_results(self.id)[0]["failed"], 1)

    def test_identical_settings_and_retained_event_types_keep_running_attempt(self):
        self.store.accept(self.config, self.envelope())
        row = self.store.next_event(self.store.subscriptions())
        self.store.start_processing(self.id, row["event_id"], row["attempt_id"], 0)
        self.store.update_instructions(self.id, "  Prepare a draft  ", 0)
        self.store.configure(self.connection, expected_local_revision=1)
        self.store.sync_settings(self.id, self.envelope(kind="control", events=["message_new"], revision=1, active=True))
        self.store.require_processing(self.id, row["event_id"], row["attempt_id"])
        self.assertEqual(self.store.pending_results(self.id), [])
        self.store.sync_settings(self.id, self.envelope(kind="control", events=["lead_created"], revision=2, active=True))
        with self.assertRaises(ValueError):
            self.store.require_processing(self.id, row["event_id"], row["attempt_id"])

    def test_server_pause_remains_cancelled_after_quick_reactivation(self):
        self.store.accept(self.config, self.envelope())
        row = self.store.next_event(self.store.subscriptions())
        self.store.start_processing(self.id, row["event_id"], row["attempt_id"], 0)
        for active in [False, True]:
            self.store.sync_settings(self.id, self.envelope(kind="control", active=active, events=self.connection["events"]))
        with self.assertRaises(ValueError):
            self.store.require_processing(self.id, row["event_id"], row["attempt_id"])

    def test_recreated_event_rejects_old_attempt_in_every_execution_write(self):
        self.store.accept(self.config, self.envelope())
        old = self.store.next_event(self.store.subscriptions())
        self.store.remove(self.id)
        self.store.configure(self.connection, "Prepare a draft")
        self.store.accept(self.config, self.envelope())
        new = self.store.next_event(self.store.subscriptions())
        with self.assertRaises(ValueError):
            self.store.start_processing(self.id, old["event_id"], old["attempt_id"], 0)
        self.store.start_processing(self.id, new["event_id"], new["attempt_id"], 0)
        with self.assertRaises(ValueError):
            self.store.prepare_reply(self.id, old["event_id"], old["attempt_id"], {"id": "dialog-1", "content": "Old reply"})
        with self.assertRaises(ValueError):
            self.store.append_report(self.id, old["event_id"], old["attempt_id"], "Old result")
        self.store.finish(self.id, old["event_id"], old["attempt_id"])
        self.store.require_processing(self.id, new["event_id"], new["attempt_id"])
        self.store.append_report(self.id, new["event_id"], new["attempt_id"], "New result")
        self.store.finish(self.id, new["event_id"], new["attempt_id"])
        self.store.reported(self.id, old["event_id"], old["attempt_id"])
        self.assertEqual(self.store.pending_results(self.id)[0]["result"], "New result")

    def test_credential_refresh_preserves_unstarted_work_and_pause(self):
        self.store.accept(self.config, self.envelope())
        self.store.pause(self.id)
        self.store.configure({**self.connection, "revision": 1, "gateway_secret": "new" * 20}, expected_local_revision=1)
        self.assertFalse(self.store.subscriptions()[self.id]["enabled"])
        self.store.resume(self.id, 2)
        row = self.store.next_event(self.store.subscriptions())
        self.assertEqual(row["event_id"], "event-1")
        self.assertEqual(row["revision"], 1)

    def test_queue_backpressure_leaves_overflow_unacknowledged(self):
        with patch.object(store_module, "MAX_PENDING", 1):
            self.store.accept(self.config, self.envelope())
            with self.assertRaisesRegex(ValueError, "full"):
                self.store.accept(self.config, self.envelope(payload={**self.envelope()["payload"], "event_id": "event-2"}))
            self.assertEqual(self.store.accept(self.config, self.envelope()), "duplicate")

    def test_only_one_gateway_can_recover_and_process_this_inbox(self):
        lock = self.store.acquire_gateway_lock()
        try:
            with self.assertRaises(ValueError):
                self.store.acquire_gateway_lock()
        finally:
            lock.close()
        self.store.acquire_gateway_lock().close()

    def test_connection_url_cannot_carry_credentials_or_insecure_remote_transport(self):
        for url in ["ws://remote.example/api/hermes-relay/" + self.id,
                    "wss://user:secret@api.example/api/hermes-relay/" + self.id,
                    self.connection["relay_url"] + "?token=secret", "wss://api.example/wrong"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                store_module.validate_connection({**self.connection, "relay_url": url})


if __name__ == "__main__":
    unittest.main()
