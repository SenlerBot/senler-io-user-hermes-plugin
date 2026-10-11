"""Check chat preparation against pinned Hermes dispatch and connection-card code.

Native turn context, target validation, card lifecycle and inline dispatcher are
unchanged. Config persistence, OAuth/network, tool discovery and UI clicks are
test doubles. No real login, model or Senler service is used.
"""
import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from test_chat import Config, chat


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def functions(path, names, namespace, *, owner=None):
    tree = ast.parse(path.read_text())
    body = next(n.body for n in tree.body if isinstance(n, ast.ClassDef) and n.name == owner) if owner else tree.body
    selected = [n for n in body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in selected} == set(names)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[])), str(path), "exec"), namespace)
    return namespace


def main(source):
    for name in ("tools", "tools.connectors", "tools.connectors.gateway", "hermes_cli", "agent"):
        module = ModuleType(name)
        module.__path__ = [str(source / name.replace(".", "/"))]
        sys.modules[name] = module
    constants = load("hermes_constants", source / "hermes_constants.py")
    turn = load("tools.connectors.turn", source / "tools/connectors/turn.py")
    targets = load("tools.connectors.targets", source / "tools/connectors/targets.py")
    threads = load("tools.thread_context", source / "tools/thread_context.py")
    threads._callback_api = lambda: ()  # Unrelated terminal/sudo/vault callbacks.
    registry = SimpleNamespace(get_definitions=lambda names, **kw: list(names))
    config = Config()
    config.profile = SimpleNamespace(get=lambda: str(constants.get_hermes_home()))
    sys.modules["hermes_cli"].config = config
    boundaries = {
        "tools.registry": SimpleNamespace(registry=registry, tool_error=lambda text: json.dumps({"error": text})),
        "tools.connectors.gateway.config": SimpleNamespace(operation_session_key=lambda value: value,
                                                           connectors_available=lambda: True),
        "tools.interrupt": SimpleNamespace(is_interrupted=lambda: False),
        "tools.tool_search_catalog": SimpleNamespace(build_catalog_listing_with_form=lambda definitions, **kw: (str(definitions), None)),
        "hermes_cli.mcp_catalog": SimpleNamespace(get_entry=lambda name: None),
        "agent.redact": SimpleNamespace(redact_sensitive_text=lambda text, **kw: text),
    }
    sys.modules.update(boundaries)
    mcp = load("tools.connectors.mcp", source / "tools/connectors/mcp.py")
    from tools.connectors import live, run
    from tools.connectors.contract import SettleReason

    run.WATCH_INTERVAL_SECONDS = 0.01
    mcp._catalog_names = lambda: []  # Senler is a custom server, not a catalog entry.
    mcp._configured_names = lambda: list(config.load_config_readonly().get("mcp_servers", {}))
    mcp._register_connected = lambda *args: (["mcp__senler_io_user__search"], "")

    class Backend:
        def __init__(self):
            self.status = "pending"
            self.error = ""
            self.starts = 0

        def start_oauth(self, name):
            assert name == "senler-io-user"
            self.starts += 1
            return SimpleNamespace(auth_url="https://login.example.test/senler", poll=lambda: {
                "status": self.status, "error": self.error, "tools": [], "discovery_error": ""})

    backend = Backend()
    mcp._default_backend = lambda: backend
    manage_ns = {"normalize_targets": targets.normalize_targets, "validate_action": targets.validate_action,
                 "MCP_ACTIONS": targets.MCP_ACTIONS, "run_mcp_operation": mcp.run_mcp_operation,
                 "tool_error": boundaries["tools.registry"].tool_error}
    manage = functions(source / "tools/connectors/tool.py", ["manage_connections"], manage_ns)["manage_connections"]
    sys.modules["tools.connectors"].manage_connections = manage
    inline = functions(source / "agent/inline_tool_executors.py",
                       ["_manage_connections", "_scope_in_connected_mcp_servers"], {"json": json})
    dispatch = functions(source / "run_agent.py", ["_execute_tool_calls"], {
        "scoped_connection_surface": turn.scoped_connection_surface,
        "agent_connection_surface": turn.agent_connection_surface,
    }, owner="AIAgent")["_execute_tool_calls"]

    with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HERMES_HOME": home}):
        config.contents = {home: {"model": {"api_key": "${UNCHANGED_KEY}"}}}
        with ThreadPoolExecutor(max_workers=1) as pool:
            prepared = []
            def sequential(*args):
                prepared.append(json.loads(pool.submit(threads.propagate_context_to_thread(
                    lambda: chat.prepare_connection({}))).result(timeout=5)))
            agent = SimpleNamespace(connection_callback=lambda payload: None, enabled_toolsets=["terminal"],
                                    session_id="senler-chat-probe", _execute_tool_calls_sequential=sequential)
            message = SimpleNamespace(tool_calls=[object()])
            dispatch(agent, message, [], "probe")
            assert prepared[-1]["created"] and prepared[-1]["status"] == "authorization_pending"
            followup = prepared[-1]["next_calls"][0]
            assert followup["tool"] == "manage_connections"
            assert config.contents[home]["model"] == {"api_key": "${UNCHANGED_KEY}"}

            for side in (False, True):
                agent.connection_callback = None
                agent.side_agent = side
                dispatch(agent, message, [], "probe")
                assert "error" in prepared[-1]
            assert config.saved == [home]

        for outcome in ("connected", "skipped", "failed", "deadline"):
            live.reset_for_tests()
            mcp._LATE_ATTEMPTS.clear()
            backend.status, backend.error = "pending", ""
            seen = []
            def callback(payload):
                seen.append(payload)
                operation = live.get(agent.session_id, payload["op_id"])
                assert payload["tool_call_id"] == "native-call"
                assert payload["targets"][0]["name"] == "senler-io-user"
                if outcome == "connected":
                    backend.status = "approved"
                elif outcome == "skipped":
                    mcp.apply_answer(operation, json.dumps({"targets": [{"name": "senler-io-user", "status": "skipped"}]}))
                elif outcome == "failed":
                    backend.status, backend.error = "error", "Synthetic OAuth failure"
                    runner = mcp._RUNNERS[operation.op_id]
                    runner.observe(operation)
                    operation.settle(SettleReason.continue_)
                else:
                    operation.deadline_at = 0
            agent.connection_callback = callback
            agent.side_agent = False
            agent.enabled_toolsets = ["terminal"]
            result = json.loads(inline["_manage_connections"](
                agent, followup["arguments"], SimpleNamespace(tool_call_id="native-call")))
            assert len(seen) == 1 and len(result["targets"]) == 1
            target = result["targets"][0]
            assert "connect_url" not in target
            if outcome == "connected":
                assert target["state"] == "connected" and target["tools"]
                assert "senler-io-user" in agent.enabled_toolsets
            else:
                assert target["state"] != "connected"
                assert not target.get("tools")
                assert "senler-io-user" not in agent.enabled_toolsets
        assert backend.starts == 4
        live.reset_for_tests()
    print("PASS: native Hermes chat context survives tool workers; background setup is rejected; "
          "returned MCP call reaches the native card callback and handles success, skip, failure and timeout")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    main(parser.parse_args().source_dir)
