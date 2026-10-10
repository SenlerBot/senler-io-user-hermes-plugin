import base64
import copy
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("senler_webhook", Path(__file__).parents[1] / "skills/senler-io-user/scripts/senler_webhook.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

PROJECT = "0193d770-849b-71e0-a8b7-0b0244d7d1f1"
BASE = {"project_id": PROJECT, "workflow": "support"}
SECRET = "whsec_" + base64.b64encode(bytes([251, 255, 239]) * 8).decode()


class Runtime:
    def __init__(self):
        self.store = {"unrelated": {"secret": "preserve", "enabled": False}}
        self.static = {}

    def settings(self):
        return {"public_base_url": "https://receiver.example", "allowed_toolsets": [helper.TOOLSET]}

    def static_routes(self):
        return self.static

    def routes(self):
        return copy.deepcopy(self.store)

    def mutate(self, callback):
        updated = copy.deepcopy(self.store)
        result = callback(updated)
        self.store = updated
        return result


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.plan = self.run_action("plan")
        self.remote = {"id": "a" * 24, "project_id": PROJECT, "status": "disabled",
                       "url": self.plan["url"], "events": ["message_new", "lead_created"],
                       "signature_scheme": "standard-webhooks"}
        self.config = {"webhook": self.remote, "webhook_secret": SECRET,
                       "expected_revision": self.plan["revision"],
                       "instructions": "Prepare a draft in Russian.", "channel_ids": ["channel-a"]}

    def run_action(self, action, now=1000, **data):
        return helper.execute(self.runtime, action, {**BASE, **data}, now)

    def receipt(self, event, **overrides):
        body = {"status": "accepted", "route": self.plan["route"], "event": event, "delivery_id": "test-" + event}
        return {"success": True, "status_code": 202, "response_body": json.dumps(body),
                "event_id": body["delivery_id"], "response_truncated": False, **overrides}

    def configure(self):
        return self.run_action("configure", **{**self.config, "expected_revision": self.run_action("plan")["revision"]})

    def verify(self):
        for event in self.remote["events"]:
            self.run_action("record-test", result=self.receipt(event))

    def test_setup_requires_every_event_and_returns_automatic_activation(self):
        self.configure()
        with self.assertRaises(ValueError):
            self.run_action("ready", webhook=self.remote)
        self.verify()
        self.assertTrue(self.run_action("ready", webhook=self.remote)["ready"])
        self.assertEqual(self.run_action("mark-active", webhook={**self.remote, "status": "active"})["phase"], "active")
        self.assertNotIn(SECRET, json.dumps(self.run_action("status")))
        self.assertEqual(self.runtime.store["unrelated"], {"secret": "preserve", "enabled": False})

    def test_restart_and_repeated_setup_reuse_binding_without_old_test_proof(self):
        self.configure()
        self.verify()
        restarted = Runtime()
        restarted.store = json.loads(json.dumps(self.runtime.store))
        self.runtime = restarted
        self.assertEqual(self.run_action("plan")["existing"]["webhook_id"], self.remote["id"])
        self.configure()
        self.assertEqual(len(self.runtime.store), 2)
        with self.assertRaises(ValueError):
            self.run_action("ready", webhook=self.remote)

    def test_ignored_failed_mismatched_and_stale_tests_do_not_enable(self):
        self.configure()
        for override in [{"status_code": 200}, {"success": False}, {"response_truncated": True},
                         {"event_id": "other"}, {"response_body": '{"status":"ignored"}'}]:
            with self.assertRaises(ValueError):
                self.run_action("record-test", result=self.receipt("message_new", **override))
        self.verify()
        with self.assertRaises(ValueError):
            self.run_action("ready", now=1601, webhook=self.remote)
        self.runtime.store[self.plan["route"]]["prompt"] = "changed elsewhere"
        with self.assertRaises(ValueError):
            self.run_action("ready", webhook=self.remote)

    def test_scope_pause_and_route_ownership(self):
        self.configure()
        route = self.runtime.store[self.plan["route"]]
        self.assertEqual(route["filters"][0], {"field": "project_id", "equals": PROJECT})
        self.assertEqual(route["toolsets"], [helper.TOOLSET])
        self.assertEqual(route["skills"], [])
        self.assertIn(helper.incoming_policy()[0], route["prompt"])
        self.assertIn("dialog_id: {dialog_id}", route["prompt"])
        self.assertIn("event_id: {event_id}", route["prompt"])
        self.assertIn(self.run_action("status")["result_search_query"], route["prompt"])
        self.assertIn("{__raw__}", route["prompt"])
        self.assertIn("senler:" + self.plan["route"] + ":{event_id}:reply", route["prompt"])
        with self.assertRaises(ValueError):
            self.run_action("pause", webhook={**self.remote, "project_id": "other"})
        self.assertFalse(self.run_action("pause", webhook=self.remote)["enabled_locally"])
        self.assertTrue(self.configure()["enabled_locally"])
        self.runtime.static[self.plan["route"]] = {}
        with self.assertRaises(ValueError):
            self.configure()

    def test_preserves_manual_route_changes(self):
        self.configure()
        self.runtime.store[self.plan["route"]]["deliver"] = "telegram"
        with self.assertRaises(ValueError):
            self.configure()
        self.run_action("pause", webhook=self.remote)
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(self.runtime.store[self.plan["route"]]["deliver"], "telegram")

    def test_receipt_retry_does_not_extend_expiry_or_verify_a_new_configuration(self):
        self.configure()
        self.verify()
        self.run_action("record-test", now=1601, result=self.receipt("message_new"))
        with self.assertRaises(ValueError):
            self.run_action("ready", now=1601, webhook=self.remote)
        self.configure()
        with self.assertRaises(ValueError):
            self.run_action("record-test", result=self.receipt("message_new"))
        self.assertEqual(self.run_action("status")["verified_events"], [])

    def test_missing_or_changed_incoming_policy_cannot_be_marked_ready(self):
        original = self.runtime.routes()
        with patch.object(helper, "incoming_policy", side_effect=ValueError("Policy missing")):
            with self.assertRaises(ValueError):
                self.configure()
        self.assertEqual(self.runtime.store, original)
        self.configure()
        self.verify()
        with patch.object(helper, "incoming_policy", return_value=("Updated policy", "other-hash")):
            with self.assertRaises(ValueError):
                self.run_action("ready", webhook=self.remote)

    def test_invalid_signature_live_remote_and_rebinding_are_rejected(self):
        for override in [{"webhook_secret": SECRET.replace("+", "-").replace("/", "_")},
                         {"webhook": {**self.remote, "status": "active"}},
                         {"webhook": {**self.remote, "signature_scheme": "senler-v1"}}]:
            with self.assertRaises(ValueError):
                self.run_action("configure", **{**self.config, **override})
        self.configure()
        with self.assertRaises(ValueError):
            self.run_action("configure", **{**self.config, "expected_revision": self.run_action("plan")["revision"],
                                           "webhook": {**self.remote, "id": "b" * 24}})

    def test_private_or_ambiguous_public_addresses_are_rejected(self):
        for value in ["http://public.example", "https://localhost", "https://192.168.1.1", "https://u:p@receiver.example", "https://receiver.example/path"]:
            with self.assertRaises(ValueError):
                helper.public_origin(value)

    def test_new_chat_can_find_owned_connections_without_knowing_a_workflow(self):
        self.configure()
        self.runtime.store["foreign"] = {"secret": "do-not-return", "senler_connection": {"owner": "other"}}
        self.runtime.store["not-a-route"] = "preserve"
        self.runtime.store["metadata-is-null"] = {"senler_connection": None}
        before = self.runtime.routes()
        self.runtime.settings = lambda: self.fail("Listing must not read provisioning settings")
        found = helper.execute(self.runtime, "connections", {"project_id": PROJECT})
        self.assertEqual(len(found["connections"]), 1)
        self.assertEqual(found["connections"][0]["workflow"], BASE["workflow"])
        self.assertEqual(found["connections"][0]["webhook_id"], self.remote["id"])
        self.assertFalse(found["remote_status_checked"])
        self.assertNotIn(SECRET, json.dumps(found))
        self.assertNotIn("do-not-return", json.dumps(found))
        self.assertEqual(self.runtime.store, before)
        other = helper.execute(self.runtime, "connections", {"project_id": "00000000-0000-4000-8000-000000000001"})
        self.assertEqual(other["connections"], [])

    def test_connection_discovery_uses_a_bounded_cursor(self):
        self.configure()
        second = copy.deepcopy(self.runtime.store[self.plan["route"]])
        second["senler_connection"]["workflow"] = "another"
        second_name = helper.route_name(PROJECT, "another")
        self.runtime.store[second_name] = second
        first = helper.execute(self.runtime, "connections", {"project_id": PROJECT, "limit": 1})
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["connections"][0]["route"], second_name)
        last = helper.execute(self.runtime, "connections", {"project_id": PROJECT, "limit": 1, "after": first["next_cursor"]})
        self.assertEqual(last["connections"][0]["route"], self.plan["route"])
        self.assertIsNone(last["next_cursor"])
        with self.assertRaises(ValueError):
            helper.execute(self.runtime, "connections", {"limit": 101})

    def test_status_and_local_pause_work_without_public_url_or_senler_access(self):
        self.configure()
        self.runtime.settings = lambda: self.fail("Control actions must not need provisioning settings")
        self.assertEqual(self.run_action("status")["webhook_id"], self.remote["id"])
        result = self.run_action("pause")
        self.assertEqual(result["phase"], "paused_locally")
        self.assertFalse(result["enabled_locally"])
        self.assertFalse(result["remote_status_checked"])
        self.assertEqual(result["verified_events"], [])
        changed_remote = {**self.remote, "url": "https://new-receiver.example", "events": ["message_new"]}
        self.assertEqual(self.run_action("pause", webhook=changed_remote)["phase"], "paused")
        self.assertTrue(self.run_action("remove", webhook_id=self.remote["id"])["removed_locally"])
        self.assertEqual(self.runtime.store["unrelated"], {"secret": "preserve", "enabled": False})

    def test_missing_public_address_still_blocks_setup(self):
        self.runtime.settings = lambda: {}
        with self.assertRaises(ValueError):
            self.run_action("plan")
        with self.assertRaises(ValueError):
            self.configure()

    def test_stale_setup_cannot_undo_a_later_pause_or_replace_another_setup(self):
        self.configure()
        old_plan = self.run_action("plan")
        self.run_action("pause")
        with self.assertRaises(ValueError):
            self.run_action("configure", **{**self.config, "expected_revision": old_plan["revision"]})
        self.assertEqual(self.run_action("status")["phase"], "paused_locally")
        self.assertFalse(self.run_action("status")["enabled_locally"])
        # A create started from an empty local store also cannot replace a binding
        # that another request has installed in the meantime.
        with self.assertRaises(ValueError):
            self.run_action("configure", **self.config)


class ProvisionTests(unittest.TestCase):
    def runtime(self, home, save=True):
        runtime = helper.HermesRuntime.__new__(helper.HermesRuntime)
        runtime.home = Path(home)
        disk = {"mcp_servers": {helper.SERVER: {"auth": "oauth"}}, "unrelated": {"keep": True},
                "platforms": {"webhook": {"enabled": False, "port": 9876, "routes": {"static": {}}}}}
        runtime.config = copy.deepcopy(disk)
        runtime.load_config = lambda: copy.deepcopy(disk)
        def save_config(value, merge_existing):
            self.assertTrue(merge_existing)
            if save:
                disk["platforms"]["webhook"].update(value["platforms"]["webhook"])
        runtime.save_config = save_config
        runtime.atomic_write = lambda path, value, mode: path.write_text(json.dumps(value))
        return runtime

    def test_preserves_flat_port_and_static_routes(self):
        with TemporaryDirectory() as home:
            runtime = self.runtime(home)
            self.assertEqual(runtime.static_routes(), {"static": {}})
            runtime.provision("https://receiver.example")
            self.assertEqual(runtime.config["platforms"]["webhook"]["extra"]["port"], 9876)
            self.assertEqual(runtime.static_routes(), {"static": {}})
            self.assertEqual(runtime.config["unrelated"], {"keep": True})
            self.assertEqual(runtime.settings()["allowed_toolsets"], [helper.TOOLSET])

    def test_refused_config_save_does_not_report_provisioning_success(self):
        with TemporaryDirectory() as home:
            runtime = self.runtime(home, save=False)
            with self.assertRaises(ValueError):
                runtime.provision("https://receiver.example")
            self.assertEqual(runtime.settings(), {})

    def test_nested_adapter_settings_override_flat_settings(self):
        actual = helper.webhook_platform({"platforms": {"webhook": {
            "port": 9876, "routes": {"flat": {}}, "extra": {"port": 8644, "routes": {"nested": {}}}}}})
        self.assertEqual(actual["extra"], {"port": 8644, "routes": {"nested": {}}})


if __name__ == "__main__":
    unittest.main()
