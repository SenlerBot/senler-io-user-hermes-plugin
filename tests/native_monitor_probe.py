"""Exercise unmodified Hermes discovery/unwrapping and delivery; no model or network."""
import argparse
import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_dir.resolve()))
    with tempfile.TemporaryDirectory(prefix="senler-native-monitor-") as directory:
        with patch.dict(os.environ, {"HERMES_HOME": directory}):
            run(Path(directory))


def run(home):
    from tools import tool_search
    from tools.registry import registry
    from agent import tool_executor
    import model_tools
    from gateway.config import GatewayConfig, PlatformConfig, Platform
    from gateway.platforms.base import SendResult

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("senler_monitor_probe", root / "__init__.py",
                                                 submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    actions = importlib.import_module(spec.name + ".senler_relay_actions")
    store_module = importlib.import_module(spec.name + ".senler_relay_store")
    notifications = importlib.import_module(spec.name + ".senler_notifications")

    name = "mcp__senler_io_user__search"
    schema = {"name": name, "description": "Senler methods search", "parameters": {
        "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}
    registry.register(name=name, toolset="mcp-senler-io-user", schema=schema, handler=lambda args, **kw: "{}")
    foreign = "mcp__other__execute"
    registry.register(name=foreign, toolset="mcp-other", schema={**schema, "name": foreign}, handler=lambda args, **kw: "{}")
    definitions = [{"type": "function", "function": schema}]
    found = json.loads(tool_search.dispatch_tool_search({"queries": ["Senler"]}, current_tool_defs=definitions))
    assert name in found["tools"], found
    described = json.loads(tool_search.dispatch_tool_describe({"names": [name]}, current_tool_defs=definitions))
    assert described["tools"][name]["parameters"] == schema["parameters"]
    agent = SimpleNamespace(enabled_toolsets=["mcp-senler-io-user"], disabled_toolsets=None)
    with patch.object(model_tools, "get_tool_definitions", return_value=definitions):
        actual, params, error = tool_executor._unwrap_tool_search_call(agent, "tool_call", {
            "calls": [{"name": name, "arguments": {"query": "вебхуки"}}]})
        assert (actual, params, error) == (name, {"query": "вебхуки"}, None)
        _, _, blocked = tool_executor._unwrap_tool_search_call(agent, "tool_call", {
            "calls": [{"name": foreign, "arguments": {"query": "forbidden"}}]})
        assert blocked
    token = actions._attempt.set(SimpleNamespace(require_active=lambda: None))
    try:
        assert actions.before_tool("tool_search", {"queries": ["Senler"]}) is None
        assert actions.before_tool("tool_describe", {"names": [name]}) is None
        assert actions.before_tool(actual, params) is None
        assert actions.before_tool("tool_call", {})["action"] == "block"
        assert actions.before_tool("terminal", {})["action"] == "block"
    finally:
        actions._attempt.reset(token)

    store = store_module.RelayStore(home)
    webhook = "a" * 24
    project = "4e576fd4-f48d-4d49-af17-4fda06c82479"
    connection = {"webhook_id": webhook, "project_id": project, "gateway_id": webhook,
                  "relay_url": "wss://example.test/api/hermes-relay/" + webhook,
                  "gateway_secret": "fixture-" + "x" * 40, "events": ["message_new"], "revision": 0}
    target = {"platform": "telegram", "chat_id": "12345", "thread_id": "77"}
    store.configure(connection, "Notify only when important", notification_target=target)
    config = store.subscriptions()[webhook]
    envelope = {"kind": "event", "webhook_id": webhook, "project_id": project, "revision": 0,
                "expires_at": "2099-01-01T00:00:00Z", "payload": {"event_id": "important", "project_id": project,
                "event_type": "message_new", "dialog_id": "fixture", "data": {}}}
    store.accept(config, envelope)
    row = store.next_event(store.subscriptions())
    store.start_processing(webhook, row["event_id"], row["attempt_id"], 0)
    store.append_report(webhook, row["event_id"], row["attempt_id"], '{"notify":true,"summary":"Требуется внимание"}')
    store.finish(webhook, row["event_id"], row["attempt_id"])
    sender = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True, message_id="native-receipt")))
    runner = SimpleNamespace(config=GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True)}),
                             adapters={Platform.TELEGRAM: sender})
    outbox = notifications.NotificationOutbox(store)
    asyncio.run(outbox.deliver_one(runner))
    sender.send.assert_awaited_once()
    call = sender.send.await_args
    assert call.args[0] == "12345" and "Требуется внимание" in call.args[1]
    assert call.kwargs["metadata"] == {"thread_id": "77"}
    assert outbox.status(webhook)["last_delivery"]["status"] == "delivered"
    store.recover()
    store.accept(config, envelope)
    asyncio.run(outbox.deliver_one(runner))
    sender.send.assert_awaited_once()
    print("PASS: native discovery/schema/unwrap, Senler-only hook scope, native delivery/thread routing and restart deduplication")


if __name__ == "__main__":
    main()
