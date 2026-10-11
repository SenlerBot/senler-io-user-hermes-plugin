"""Operator instruction commands use the saved subscription without OAuth or config writes."""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class UnscopedSecretError(RuntimeError):
    pass


class RelaySetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        spec = importlib.util.spec_from_file_location("senler_setup_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        plugin = importlib.util.module_from_spec(spec)
        modules = {
            spec.name: plugin,
            "hermes_constants": SimpleNamespace(get_hermes_home=lambda: Path(self.temp.name)),
            "hermes_cli.config": SimpleNamespace(load_config_readonly=Mock(side_effect=AssertionError("Unexpected config read")),
                save_config=Mock(side_effect=AssertionError("Unexpected config write"))),
            "agent.secret_scope": SimpleNamespace(UnscopedSecretError=UnscopedSecretError, get_secret=Mock(return_value=None)),
        }
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        spec.loader.exec_module(plugin)
        self.setup = importlib.import_module("senler_setup_test.senler_relay_setup")
        self.store = self.setup.RelayStore(self.temp.name)
        self.id = "b" * 24
        self.connection = {"webhook_id": self.id, "project_id": str(uuid4()), "gateway_id": self.id,
            "relay_url": "wss://api.example.test/api/hermes-relay/" + self.id, "gateway_secret": "fixture-" + "x" * 40,
            "events": ["message_new"], "revision": 0}
        self.store.configure(self.connection, "Отвечай на вопросы о доставке")

    def command(self, action, data, *, enable=False):
        parser = argparse.ArgumentParser()
        self.setup.configure_parser(parser)
        args = parser.parse_args([action] + (["--enable-automation"] if enable else []))
        with patch.object(sys, "stdin", io.StringIO(json.dumps(data))), redirect_stdout(io.StringIO()) as output, \
                redirect_stderr(io.StringIO()) as errors:
            code = self.setup.handle_command(args)
        return code, output.getvalue(), errors.getvalue()

    def test_read_and_update_rules_preserve_connection_and_pause_without_oauth(self):
        self.store.pause(self.id)
        code, output, errors = self.command("instructions", {"webhook_id": self.id})
        self.assertEqual((code, errors), (0, ""))
        saved = json.loads(output)
        self.assertEqual(saved["instructions"], "Отвечай на вопросы о доставке")
        self.assertNotIn("gateway_secret", output)
        updated = saved["instructions"] + "; только в рабочее время"
        code, output, errors = self.command("update-instructions", {"webhook_id": self.id,
            "instructions": updated, "expected_local_revision": saved["local_revision"]})
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["instructions"], updated)
        self.assertFalse(json.loads(output)["enabled"])
        self.assertEqual(self.store.subscriptions()[self.id]["gateway_secret"], self.connection["gateway_secret"])
        code, output, errors = self.command("update-instructions", {"webhook_id": self.id,
            "instructions": "Stale edit", "expected_local_revision": saved["local_revision"]})
        self.assertEqual((code, output), (1, ""))
        self.assertIn("read its current instructions", errors)
        self.assertEqual(self.store.instructions(self.id)["instructions"], updated)

    def test_instruction_command_rejects_unknown_fields_and_invalid_text(self):
        for data in [
            {"webhook_id": self.id, "instructions": "", "expected_local_revision": 0},
            {"webhook_id": self.id, "instructions": "x" * 8001, "expected_local_revision": 0},
            {"webhook_id": self.id, "instructions": "Valid", "expected_local_revision": 0, "gateway_secret": "do-not-print"},
        ]:
            with self.subTest(fields=list(data)):
                code, output, errors = self.command("update-instructions", data)
                self.assertEqual((code, output), (1, ""))
                self.assertNotIn("do-not-print", errors)
        self.assertEqual(self.store.instructions(self.id)["local_revision"], 0)

    def test_proxy_mode_blocks_configure_and_resume_before_any_settings_change(self):
        self.store.pause(self.id)
        saved = self.store.subscriptions()
        config_api = sys.modules["hermes_cli.config"]
        secret = sys.modules["agent.secret_scope"].get_secret
        private_url = "https://private-hermes.example.test"
        for source in ["config", "profile", "unscoped_environment"]:
            config_api.load_config_readonly.side_effect = None
            config_api.load_config_readonly.return_value = {"gateway": {"proxy_url": private_url}} if source == "config" else {}
            secret.side_effect = UnscopedSecretError() if source == "unscoped_environment" else None
            secret.return_value = private_url if source == "profile" else None
            with patch.dict(os.environ, {"GATEWAY_PROXY_URL": private_url if source == "unscoped_environment" else ""}):
                for action in ["configure", "resume"]:
                    with self.subTest(source=source, action=action):
                        data = {"webhook_id": self.id, "expected_local_revision": saved[self.id]["local_revision"]}
                        if action == "configure":
                            data = {"connection": self.connection, "expected_local_revision": saved[self.id]["local_revision"]}
                        code, output, errors = self.command(action, data, enable=True)
                        self.assertEqual((code, output), (1, ""))
                        self.assertIn("same Hermes instance", errors)
                        self.assertNotIn(private_url, errors)
                        self.assertEqual(self.store.subscriptions(), saved)
                        config_api.save_config.assert_not_called()

    def test_proxy_check_respects_profile_scope_and_accepts_cloud_models(self):
        secret = sys.modules["agent.secret_scope"].get_secret
        config = {"gateway": {"proxy_url": "  "}, "model": {"base_url": "https://cloud-model.example.test"}}
        with patch.dict(os.environ, {"GATEWAY_PROXY_URL": "https://another-profile.example.test"}):
            self.setup.require_supported_gateway(config)
            secret.assert_called_with("GATEWAY_PROXY_URL")
            secret.side_effect = UnscopedSecretError()
            with self.assertRaisesRegex(ValueError, "Gateway proxy mode"):
                self.setup.require_supported_gateway(config)
        with patch.dict(os.environ, {"GATEWAY_PROXY_URL": " "}):
            self.setup.require_supported_gateway(config)
        secret.side_effect = RuntimeError("Profile lookup failed")
        with self.assertRaises(RuntimeError):
            self.setup.require_supported_gateway({})

    def test_status_explains_incompatible_mode_without_secrets_or_settings_changes(self):
        self.store.pause(self.id)
        saved = self.store.subscriptions()
        config_api = sys.modules["hermes_cli.config"]
        secret = sys.modules["agent.secret_scope"].get_secret
        private_url = "https://private-hermes.example.test"
        config_api.load_config_readonly.side_effect = None
        for source in ["config", "profile", "unscoped_environment", "multiplex", "compatible"]:
            with self.subTest(source=source):
                config_api.load_config_readonly.return_value = (
                    {"gateway": {"proxy_url": private_url}} if source == "config" else
                    {"gateway": {"multiplex_profiles": True}} if source == "multiplex" else {})
                secret.side_effect = UnscopedSecretError() if source == "unscoped_environment" else None
                secret.return_value = private_url if source == "profile" else None
                with patch.dict(os.environ, {"GATEWAY_PROXY_URL": private_url}):
                    code, output, errors = self.command("status", {})
                self.assertEqual((code, errors), (0, ""))
                status = json.loads(output)
                reason = status["processing_blocked_reason"]
                if source == "compatible":
                    self.assertIsNone(reason)
                elif source == "multiplex":
                    self.assertIn("one gateway per Hermes profile", reason)
                else:
                    self.assertIn("same Hermes instance", reason)
                self.assertFalse(status["subscriptions"][0]["enabled"])
                self.assertNotIn(private_url, output)
                self.assertNotIn(self.connection["gateway_secret"], output)
                self.assertEqual(self.store.subscriptions(), saved)
        config_api.save_config.assert_not_called()

    def test_supported_resume_keeps_existing_mcp_and_platform_settings(self):
        self.store.pause(self.id)
        config_api = sys.modules["hermes_cli.config"]
        config_api.load_config_readonly.side_effect = None
        config_api.load_config_readonly.return_value = {}
        code, output, errors = self.command("resume", {"webhook_id": self.id,
            "expected_local_revision": self.store.instructions(self.id)["local_revision"]}, enable=True)
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["local_action"], "resume")
        self.assertTrue(self.store.subscriptions()[self.id]["enabled"])
        config_api.save_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
