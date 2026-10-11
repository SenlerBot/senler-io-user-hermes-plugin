import copy
from contextvars import ContextVar
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
PACKAGE = "senler_chat_test_plugin"
spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py",
                                            submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE] = plugin
spec.loader.exec_module(plugin)
chat = importlib.import_module(PACKAGE + ".senler_chat")
actions = importlib.import_module(PACKAGE + ".senler_relay_actions")


class Config:
    """Hermes API boundary: reads are detached, resolved profile is task-local."""
    def __init__(self, contents=None):
        self._CONFIG_LOCK = threading.RLock()
        self.profile = ContextVar("chat_test_profile", default="default")
        self.contents = {"default": copy.deepcopy(contents or {})}
        self.saved = []
        self.managed = False

    def require_readable_config_before_write(self):
        assert self._CONFIG_LOCK._is_owned()
        return copy.deepcopy(self.contents[self.profile.get()])

    def load_config_readonly(self):
        # Return the shared dict to catch writes through the read-only cache.
        return self.contents[self.profile.get()]

    def is_managed(self):
        return self.managed

    def save_config(self, value):
        assert self._CONFIG_LOCK._is_owned()
        assert value is not self.contents[self.profile.get()]
        self.saved.append(self.profile.get())
        self.contents[self.profile.get()] = copy.deepcopy(value)


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.surface = ContextVar("chat_test_surface", default="card")
        turn = SimpleNamespace(CARD="card", connection_surface=self.surface.get)
        hermes = ModuleType("hermes_cli")
        hermes.config = self.config
        self.modules = patch.dict(sys.modules, {"hermes_cli": hermes, "tools.connectors.turn": turn})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.shell = patch.object(subprocess, "run", side_effect=AssertionError("No OAuth subprocess in chat"))
        self.shell.start()
        self.addCleanup(self.shell.stop)

    def call(self, args=None):
        return json.loads(chat.prepare_connection({} if args is None else args))

    def entry(self, **kwargs):
        defaults = chat.connection_defaults()
        defaults.pop("name")
        return {**defaults, **kwargs}

    def test_new_connection_preserves_settings_and_requests_native_authorize(self):
        prior = {"model": {"api_key": "${PROVIDER_KEY}"},
                 "mcp_servers": {"other": {"command": "keep", "env": {"KEY": "${OTHER_KEY}"}}}}
        self.config.contents["default"] = copy.deepcopy(prior)
        result = self.call()
        self.assertEqual(result["status"], "authorization_pending")
        self.assertTrue(result["created"])
        self.assertEqual(result["next_calls"], [{"tool": "manage_connections", "arguments": {
            "action": "authorize", "connectors": [{"name": "senler-io-user", "mcp": True}]}}])
        self.assertEqual(self.config.contents["default"], {
            **prior, "mcp_servers": {**prior["mcp_servers"], "senler-io-user": self.entry()}})
        self.assertEqual(self.config.saved, ["default"])
        self.assertNotIn("connected", result)

    def test_repeated_setup_never_rewrites_settings_or_claims_authorization(self):
        existing = self.entry(connect_timeout=123, tools={"include": ["search"]},
                              oauth={"scope": "chosen"}, headers={"X-Custom": "${CUSTOM_HEADER}"})
        self.config.contents["default"] = {"mcp_servers": {"senler-io-user": existing}}
        before = copy.deepcopy(self.config.contents)
        for _ in range(2):
            result = self.call()
            self.assertFalse(result["created"])
            self.assertEqual(result["status"], "authorization_pending")
        self.assertEqual(self.config.contents, before)
        self.assertEqual(self.config.saved, [])

    def test_disabled_entry_is_preserved_and_native_enable_precedes_authorize(self):
        existing = self.entry(enabled=False, tools={"include": ["search"]})
        self.config.contents["default"] = {"mcp_servers": {"senler-io-user": existing}}
        result = self.call()
        self.assertEqual([call["arguments"]["action"] for call in result["next_calls"]], ["enable", "authorize"])
        self.assertEqual(self.config.contents["default"]["mcp_servers"]["senler-io-user"], existing)
        self.assertEqual(self.config.saved, [])

    def test_conflicting_and_malformed_entries_are_not_overwritten(self):
        for existing in (None, [], {"url": "https://other.invalid", "auth": "oauth"},
                         self.entry(auth="header"), self.entry(command="other"), self.entry(enabled=0)):
            with self.subTest(existing=existing):
                self.config.contents["default"] = {"mcp_servers": {"senler-io-user": existing}}
                before = copy.deepcopy(self.config.contents)
                self.assertIn("incompatible", self.call()["error"])
                self.assertEqual(self.config.contents, before)
        self.assertEqual(self.config.saved, [])

    def test_background_surfaces_cannot_change_configuration(self):
        for surface in ("side", "link", "unknown"):
            self.surface.set(surface)
            result = self.call()
            self.assertIn("interactive Hermes chat", result["error"])
        self.assertEqual(self.config.contents, {"default": {}})
        self.assertEqual(self.config.saved, [])

    def test_relay_context_is_blocked_even_if_a_card_context_was_inherited(self):
        token = actions._attempt.set(object())
        try:
            self.assertIn("error", self.call())
        finally:
            actions._attempt.reset(token)
        self.assertEqual(self.config.saved, [])

    def test_no_model_supplied_url_or_profile_is_accepted(self):
        for args in ({"url": "https://other.invalid"}, {"profile": "other"}, {"force": True}, []):
            self.assertIn("takes no arguments", self.call(args)["error"])
        self.assertEqual(self.config.saved, [])

    def test_profile_is_resolved_for_each_call_including_parallel_calls(self):
        self.config.contents.update({"work": {"keep": "work"}, "training": {"keep": "training"}})
        results = {}
        def connect_profile(name):
            token = self.config.profile.set(name)
            try:
                results[name] = self.call()
            finally:
                self.config.profile.reset(token)
        threads = [threading.Thread(target=connect_profile, args=(name,)) for name in ("work", "training")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(set(results), {"work", "training"})
        self.assertTrue(all(result.get("created") for result in results.values()))
        self.assertEqual(self.config.contents["default"], {})
        for name in results:
            self.assertEqual(self.config.contents[name], {"keep": name, "mcp_servers": {"senler-io-user": self.entry()}})

    def test_managed_configuration_is_not_written(self):
        self.config.managed = True
        self.assertIn("managed settings", self.call()["error"])
        self.assertEqual(self.config.saved, [])

    def test_read_failure_and_write_failure_cannot_leak_secrets_or_claim_success(self):
        for method in ("require_readable_config_before_write", "save_config"):
            with self.subTest(method=method), patch.object(self.config, method, side_effect=OSError("private-secret")):
                result = self.call()
                self.assertIn("error", result)
                self.assertNotIn("private-secret", json.dumps(result))
                self.assertNotIn("next_calls", result)

    def test_silently_rejected_save_is_not_success(self):
        with patch.object(self.config, "save_config"):
            self.assertIn("did not save", self.call()["error"])

    def test_malformed_root_or_mcp_mapping_is_not_overwritten(self):
        for value in ([], {"mcp_servers": []}, {"mcp_servers": None}):
            with patch.object(self.config, "require_readable_config_before_write", return_value=copy.deepcopy(value)), \
                    patch.object(self.config, "load_config_readonly", return_value=value):
                self.assertIn("error", self.call())
        self.assertEqual(self.config.saved, [])

    def test_stale_loaded_config_cannot_replace_a_saved_entry(self):
        self.config.contents["default"] = {"mcp_servers": {"senler-io-user": self.entry()}}
        with patch.object(self.config, "load_config_readonly", return_value={}):
            self.assertIn("saved entry was preserved", self.call()["error"])
        self.assertEqual(self.config.saved, [])

    def test_missing_hermes_connection_support_does_not_write(self):
        with patch.dict(sys.modules, {"tools.connectors.turn": None}):
            self.assertIn("Update Hermes", self.call()["error"])
        with patch.dict(sys.modules, {"hermes_cli": SimpleNamespace(config=SimpleNamespace())}):
            self.assertIn("Update Hermes", self.call()["error"])
        self.assertEqual(self.config.saved, [])


if __name__ == "__main__":
    unittest.main()
