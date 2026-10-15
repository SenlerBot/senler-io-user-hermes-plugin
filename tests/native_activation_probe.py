"""Exercise Hermes's actual enable/reload path and MCP listing in isolated profiles."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch
import webbrowser


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_dir.resolve()))
    with tempfile.TemporaryDirectory(prefix="senler-native-activation-") as directory:
        with patch.dict(os.environ, {"HERMES_HOME": directory}):
            run(Path(directory))


def run(root):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli import config
    from hermes_cli.plugins import get_plugin_manager
    from hermes_cli.plugins_activation import load_and_go_live
    from tools import mcp_tool_discovery
    from tools.mcp_tool_config import _load_mcp_config

    source = Path(__file__).resolve().parents[1]
    name = "senler-io-user"
    defaults = json.loads((source / "mcp-server.json").read_text())
    defaults.pop("name")

    @contextmanager
    def profile(home):
        token = set_hermes_home_override(home)
        try:
            yield
        finally:
            reset_hermes_home_override(token)

    def install_fixture(home, settings):
        destination = home / "plugins" / name
        destination.mkdir(parents=True)
        for path in source.glob("*.py"):
            shutil.copy2(path, destination / path.name)
        for filename in ("plugin.yaml", "mcp-server.json"):
            shutil.copy2(source / filename, destination / filename)
        shutil.copytree(source / "skills", destination / "skills")
        with profile(home):
            config.save_config(settings)

    first, second = root / "first", root / "second"
    install_fixture(first, {"plugins": {"enabled": []}, "mcp_servers": {
        "other": {"url": "https://other.invalid/mcp", "enabled": False}}})
    existing = {**defaults, "enabled": False, "connect_timeout": 123, "tools": {"include": ["search"]}}
    install_fixture(second, {"plugins": {"enabled": [name]}, "mcp_servers": {name: existing}})

    with patch.object(subprocess, "run") as run_command, patch.object(subprocess, "Popen") as popen, \
            patch.object(webbrowser, "open") as browser, \
            patch.object(mcp_tool_discovery, "_connect_server") as connect:
        with profile(first):
            manager = get_plugin_manager()
            manager.discover_and_load()
            assert name not in _load_mcp_config(), "A downloaded but disabled plugin must not configure MCP"
            raw = config.require_readable_config_before_write()
            raw["plugins"]["enabled"] = [name]
            config.save_config(raw)
            activation = load_and_go_live(name)
            assert activation and activation["name"] == name, activation
            assert _load_mcp_config()[name] == defaults
            rows = {row["name"]: row for row in mcp_tool_discovery.get_mcp_status()}
            assert rows[name]["status"] == "configured" and rows[name]["connected"] is False, rows
            raw = config.require_readable_config_before_write()
            assert raw["plugins"]["enabled"] == [name]
            assert raw["mcp_servers"]["other"]["enabled"] is False
            saved = (first / "config.yaml").read_bytes()
            assert load_and_go_live(name)
            assert (first / "config.yaml").read_bytes() == saved

        with profile(second):
            saved_second = (second / "config.yaml").read_bytes()
            assert load_and_go_live(name)
            assert _load_mcp_config()[name] == existing
            assert (second / "config.yaml").read_bytes() == saved_second
            assert (first / "config.yaml").read_bytes() == saved

        with profile(first):
            raw = config.require_readable_config_before_write()
            del raw["mcp_servers"][name]
            config.save_config(raw)
            assert load_and_go_live(name)
            assert name not in _load_mcp_config(), "Reload must respect a user-removed connection"

        run_command.assert_not_called()
        popen.assert_not_called()
        browser.assert_not_called()
        connect.assert_not_called()

    with profile(first):
        get_plugin_manager().unload()
    with profile(second):
        get_plugin_manager().unload()
    print("PASS: native activation adds MCP before chat; status remains configured, not authorized; "
          "reload/deletion/disabled settings and profile isolation preserved; no connection, browser or subprocess.")


if __name__ == "__main__":
    main()
