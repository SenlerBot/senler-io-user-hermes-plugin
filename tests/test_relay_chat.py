"""Interactive Relay setup uses the same local implementation as the CLI."""
import copy
import importlib
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_relay_setup as setup_tests


class RelayChatTests(unittest.TestCase):
    def setUp(self):
        setup_tests.RelaySetupTests.setUp(self)
        self.surface = "card"
        self.env = {}
        self.platforms = {"telegram": SimpleNamespace(enabled=True)}
        modules = {
            "tools.connectors.turn": SimpleNamespace(CARD="card", SIDE="side", connection_surface=lambda: self.surface),
            "gateway.session_context": SimpleNamespace(get_session_env=lambda name, default="": self.env.get(name, default),
                session_is_messaging_surface=lambda: self.env.get("HERMES_SESSION_PLATFORM") == "telegram",
                async_delivery_supported=lambda: True),
            "gateway.config": SimpleNamespace(Platform=lambda name: name,
                load_gateway_config=lambda: SimpleNamespace(platforms=self.platforms)),
            "tools.send_message_targets": SimpleNamespace(resolve_send_target=lambda platform, ref: (ref, None, None)),
        }
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, action, data=None, **kwargs):
        return json.loads(self.setup.manage_relay({"action": action, "data": data or {}, **kwargs}))

    def test_native_tool_reads_and_edits_without_shell_or_authorization(self):
        with patch("subprocess.run", side_effect=AssertionError("No shell needed")):
            saved = self.call("instructions", {"webhook_id": self.id})
            result = self.call("update-instructions", {"webhook_id": self.id, "instructions": "Только важные события",
                "expected_local_revision": saved["local_revision"]})
        self.assertEqual(result["instructions"], "Только важные события")
        self.assertNotIn(self.connection["gateway_secret"], json.dumps(result))
        sys.modules["hermes_cli.config"].save_config.assert_not_called()

    def test_background_and_side_agents_cannot_reconfigure_monitor(self):
        before = self.store.subscriptions()
        for surface, cron, attempt in [("side", "", False), ("link", "", False),
                                       ("card", "1", False), ("card", "", True)]:
            self.surface = surface
            self.env["HERMES_CRON_SESSION"] = cron
            actions = importlib.import_module("senler_setup_test.senler_relay_actions")
            token = actions._attempt.set(object() if attempt else None)
            try:
                self.assertIn("interactive chat", self.call("pause", {"webhook_id": self.id})["error"])
            finally:
                actions._attempt.reset(token)
        self.assertEqual(before, self.store.subscriptions())

    def test_desktop_origin_is_rejected_without_changing_saved_subscription(self):
        before = self.store.subscriptions()
        self.env["HERMES_SESSION_SOURCE"] = "desktop"
        result = self.call("resolve-notification-target", {"target": "origin"})
        self.assertIn("cannot receive", result["error"])
        self.assertEqual(before, self.store.subscriptions())

    def test_messaging_origin_preserves_actual_recipient_and_thread(self):
        self.env.update(HERMES_SESSION_PLATFORM="telegram", HERMES_SESSION_CHAT_ID="12345", HERMES_SESSION_THREAD_ID="17")
        result = self.call("resolve-notification-target", {"target": "origin"})
        self.assertEqual(result["target"], {"platform": "telegram", "chat_id": "12345", "thread_id": "17"})
        self.assertFalse(result["delivery_verified"])

    def test_unknown_or_disabled_targets_and_extra_fields_fail_before_writes(self):
        self.platforms["telegram"].enabled = False
        self.assertIn("Enable", self.call("resolve-notification-target", {"target": "telegram:12345"})["error"])
        self.assertIn("error", self.call("resolve-notification-target", {"target": "bot-chat:other"}))
        self.assertIn("error", self.call("resolve-notification-target", {"target": "telegram"}))
        result = self.call("pause", {"webhook_id": self.id, "ignored": "private-value"})
        self.assertIn("error", result)
        self.assertNotIn("private-value", json.dumps(result))
        self.assertTrue(self.store.subscriptions()[self.id]["enabled"])

    def test_configure_persists_target_and_policy_through_application_path(self):
        config_api = sys.modules["hermes_cli.config"]
        connection_api = importlib.import_module("senler_setup_test.senler_connection")
        defaults = connection_api.connection_defaults()
        config = {"mcp_servers": {defaults["name"]: {key: value for key, value in defaults.items() if key != "name"}}}
        config_api.load_config_readonly.side_effect = lambda: copy.deepcopy(config)
        config_api.save_config.side_effect = lambda value, **kwargs: config.update(value)
        result = self.call("configure", {"connection": self.connection, "instructions": "Важное — мне, обычное — тихо",
            "notification_target": "telegram:12345", "expected_local_revision": 0}, enable_automation=True)
        self.assertNotIn("error", result)
        self.assertFalse(result["processing_verified"])
        self.assertFalse(result["notification_delivery_verified"])
        self.assertEqual(self.store.instructions(self.id)["notification_target"]["chat_id"], "12345")
        self.assertTrue(config["platforms"]["senler-io"]["enabled"])
        proof = self.call("test-notification", {"webhook_id": self.id})
        self.assertEqual(proof["status"], "queued")
        self.assertNotIn(self.connection["gateway_secret"], json.dumps(result))

    def test_native_read_error_is_not_exposed_to_model(self):
        with patch.object(self.setup, "run_action", side_effect=RuntimeError("secret-value")):
            result = self.call("status")
        self.assertNotIn("secret-value", json.dumps(result))
        self.assertIn("error", result)

    def test_recipient_change_preserves_pause_and_credentials_without_rebinding(self):
        self.store.pause(self.id)
        before = self.store.subscriptions()[self.id]
        result = self.call("update-notification-target", {"webhook_id": self.id,
            "notification_target": "telegram:12345", "expected_local_revision": before["local_revision"]})
        self.assertEqual(result["notification_target"]["chat_id"], "12345")
        self.assertFalse(result["enabled"])
        self.assertEqual(self.store.subscriptions()[self.id]["gateway_secret"], before["gateway_secret"])
        stale = self.call("update-notification-target", {"webhook_id": self.id,
            "notification_target": None, "expected_local_revision": before["local_revision"]})
        self.assertIn("error", stale)
        sys.modules["hermes_cli.config"].save_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
