import argparse
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("senler_connection_test_module", ROOT / "senler_connection.py")
connection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connection)


class Runtime:
    def __init__(self, servers=None, interactive=True, save_on_add=True, results=None):
        self.configured = copy.deepcopy(servers or {})
        self.is_interactive = interactive
        self.save_on_add = save_on_add
        self.results = results or {}
        self.calls = []

    def servers(self):
        return copy.deepcopy(self.configured)

    def interactive(self):
        return self.is_interactive

    def invoke(self, arguments):
        self.calls.append(arguments)
        code = self.results.get(arguments[0], 0)
        if arguments[0] == "add" and self.save_on_add and code == 0:
            config = connection.connection_defaults()
            name = config.pop("name")
            self.configured[name] = {**config, "enabled": True}
        return code


class ConnectionTests(unittest.TestCase):
    def saved(self, **overrides):
        config = connection.connection_defaults()
        name = config.pop("name")
        return {name: {**config, "enabled": True, **overrides}}

    def call(self, runtime, **kwargs):
        with redirect_stdout(io.StringIO()) as output:
            code = connection.connect(runtime, **kwargs)
        return code, output.getvalue()

    def test_first_connection_uses_bundled_endpoint_and_native_oauth(self):
        runtime = Runtime(servers={"unrelated": {"command": "keep-me"}})
        code, output = self.call(runtime)
        defaults = connection.connection_defaults()
        self.assertEqual(runtime.calls, [[
            "add", "senler-io-user", "--url", defaults["url"], "--auth", "oauth",
            "--connect-timeout", "300"], ["test", "senler-io-user"]])
        self.assertEqual(code, 0)
        self.assertIn("connection verified", output)
        self.assertEqual(runtime.configured["unrelated"], {"command": "keep-me"})

    def test_existing_connection_keeps_filters_and_does_not_reauthorize(self):
        original = self.saved(tools={"include": ["search"]}, connect_timeout=123)
        runtime = Runtime(servers=original)
        self.assertEqual(self.call(runtime)[0], 0)
        self.assertEqual(runtime.calls, [["test", "senler-io-user"]])
        self.assertEqual(runtime.configured, original)

    def test_explicit_reauthorization_still_requires_successful_test(self):
        runtime = Runtime(servers=self.saved(), results={"test": 1})
        code, output = self.call(runtime, reauthorize=True)
        self.assertEqual(runtime.calls, [["login", "senler-io-user"], ["test", "senler-io-user"]])
        self.assertEqual(code, 1)
        self.assertNotIn("connection verified", output)

    def test_cancelled_add_with_zero_exit_does_not_claim_success(self):
        runtime = Runtime(save_on_add=False)
        with self.assertRaisesRegex(ValueError, "not saved"):
            self.call(runtime)
        self.assertEqual([x[0] for x in runtime.calls], ["add"])

    def test_native_failure_stops_before_followup(self):
        for action, runtime, kwargs in (
            ("add", Runtime(results={"add": 7}), {}),
            ("login", Runtime(servers=self.saved(), results={"login": 7}), {"reauthorize": True}),
        ):
            with self.subTest(action=action):
                self.assertEqual(self.call(runtime, **kwargs)[0], 7)
                self.assertEqual([x[0] for x in runtime.calls], [action])

    def test_conflicting_and_disabled_connections_are_preserved(self):
        for config in (None, {"command": "other"}, {"url": "https://other.example", "auth": "oauth"},
                       self.saved(auth="header")["senler-io-user"],
                       self.saved(enabled=False)["senler-io-user"]):
            with self.subTest(config=config):
                runtime = Runtime(servers={"senler-io-user": config})
                before = runtime.servers()
                with self.assertRaises(ValueError):
                    self.call(runtime)
                self.assertEqual(runtime.calls, [])
                self.assertEqual(runtime.servers(), before)

    def test_unattended_connect_never_starts_browser_or_cli(self):
        for servers in ({}, self.saved()):
            runtime = Runtime(servers=servers, interactive=False)
            with self.assertRaisesRegex(ValueError, "interactive terminal"):
                self.call(runtime)
            self.assertEqual(runtime.calls, [])

    def test_check_never_creates_a_connection(self):
        runtime = Runtime()
        with self.assertRaisesRegex(ValueError, "not connected"):
            connection.check(runtime)
        self.assertEqual(runtime.calls, [])
        runtime = Runtime(servers=self.saved(), interactive=False, results={"test": 1})
        self.assertEqual(connection.check(runtime), 1)
        self.assertEqual(runtime.calls, [["test", "senler-io-user"]])

    def test_native_cli_uses_parent_interpreter_and_profile_without_changing_env(self):
        runtime = connection.NativeRuntime("C:/Hermes Profiles/training", lambda: {})
        old_home = os.environ.get("HERMES_HOME")
        with patch.object(connection.subprocess, "run", return_value=subprocess.CompletedProcess([], 3)) as run:
            self.assertEqual(runtime.invoke(["test", "senler-io-user"]), 3)
        args, kwargs = run.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "hermes_cli.main", "mcp", "test", "senler-io-user"])
        self.assertEqual(kwargs["env"]["HERMES_HOME"], "C:/Hermes Profiles/training")
        self.assertFalse(kwargs["check"])
        self.assertEqual(os.environ.get("HERMES_HOME"), old_home)

    def test_corrupt_config_cannot_be_treated_as_empty(self):
        for config in (None, [], {"mcp_servers": []}):
            runtime = connection.NativeRuntime("unused", lambda: config)
            with self.assertRaises(ValueError):
                runtime.servers()

    def test_native_registration_is_passive_and_exposes_skills_and_connection_cli(self):
        registered = {"skills": [], "commands": []}

        class Context:
            def register_skill(self, name, path, description=""):
                registered["skills"].append((name, Path(path), description))

            def register_cli_command(self, name, **kwargs):
                registered["commands"].append((name, kwargs))

        spec = importlib.util.spec_from_file_location("senler_native_test_plugin", ROOT / "__init__.py",
                                                    submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            with patch.object(subprocess, "run", side_effect=AssertionError("CLI invoked during registration")):
                spec.loader.exec_module(module)
                module.register(Context())
            self.assertEqual({x[0] for x in registered["skills"]}, {"senler-io-user", "senler-io-incoming"})
            self.assertTrue(all(path.is_file() and description for _, path, description in registered["skills"]))
            self.assertEqual([x[0] for x in registered["commands"]], ["senler"])
            parser = argparse.ArgumentParser()
            registered["commands"][0][1]["setup_fn"](parser)
            self.assertEqual(parser.parse_args(["connect"]).senler_action, "connect")
            self.assertTrue(parser.parse_args(["connect", "--reauthorize"]).reauthorize)
            self.assertEqual(parser.parse_args(["check"]).senler_action, "check")
        finally:
            for key in list(sys.modules):
                if key == spec.name or key.startswith(spec.name + "."):
                    del sys.modules[key]


if __name__ == "__main__":
    unittest.main()
