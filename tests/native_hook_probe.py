"""Exercise Senler scope through pinned Hermes gateway/tool/hook worker boundaries.

No model, MCP, OAuth or customer traffic. Load unchanged upstream dispatch and
thread-context modules; compile the gateway's executor method in isolation to
avoid booting the full gateway. Only unrelated callback/config services are fake.
"""
import argparse
import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import importlib.util
import logging
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_relay_adapter import FakeGatewayBase, RelayAdapterTests


def source_path(root, name):
    path = root / name
    return path if path.is_file() else root / name.replace("/", "_")


def load_module(root, path, name):
    spec = importlib.util.spec_from_file_location(name, source_path(root, path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def native_method(root, file, owner, name):
    path = source_path(root, file)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    runner = tree if owner is None else next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner)
    method = next(node for node in runner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.name == name)
    namespace = {"asyncio": asyncio, "copy_context": copy_context, "logger": logging.getLogger(__name__),
                 "Platform": SimpleNamespace(TELEGRAM="telegram", MATRIX="matrix"), "SendResult": SimpleNamespace}
    annotations = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    tree = ast.fix_missing_locations(ast.Module(body=[annotations, method], type_ignores=[]))
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace[method.name]


async def startup_probe(root):
    case = RelayAdapterTests()
    await case.asyncSetUp()
    try:
        await case.start_attempt("interrupted")
        case.store.recover()
        intake = native_method(root, "gateway/platforms/base.py", "BasePlatformAdapter", "handle_message")
        resume = native_method(root, "gateway/run_startup.py", "GatewayStartupMixin", "_run_startup_resume_event")
        case.adapter._message_handler = True
        case.adapter._drop_unresolved = lambda event: False
        case.adapter._active_sessions = {}
        case.adapter._start_session_processing = Mock(return_value=True)
        restored = SimpleNamespace(text="", internal=True, allow_gateway_control=False, metadata={},
            message_id="interrupted", source=SimpleNamespace(platform="senler-io", chat_id=case.id + ":interrupted"))
        sentinel = object()
        runner = SimpleNamespace(_peek_session_state=lambda key: SimpleNamespace(turn=SimpleNamespace(agent=sentinel)),
                                 _release_running_agent_state=Mock())
        with patch.object(FakeGatewayBase, "handle_message", intake):
            with patch.dict(sys.modules, {"gateway.run": SimpleNamespace(_AGENT_PENDING_SENTINEL=sentinel)}):
                await resume(runner, case.adapter, restored, "restored-session")
            assert not restored._gateway_accepted
            case.adapter._start_session_processing.assert_not_called()
            runner._release_running_agent_state.assert_called_once_with("restored-session")
            assert case.store.pending_results(case.id)[0]["failed"] == 1
            row = await case.start_attempt("pending")
            case.adapter.current = SimpleNamespace(message_id="pending", allow_gateway_control=False, metadata={},
                source=SimpleNamespace(platform="senler-io", chat_id=case.id + ":pending"))
            with case.actions.processing_attempt(case.adapter, row):
                case.gateway_config["gateway"] = {"proxy_url": "https://synthetic-remote.example.test"}
                await case.adapter.handle_message(case.adapter.current)
                assert not case.adapter.current._gateway_accepted
                case.adapter._start_session_processing.assert_not_called()
                case.gateway_config.clear()
                await case.adapter.handle_message(case.adapter.current)
            assert case.adapter.current._gateway_accepted
            case.adapter._start_session_processing.assert_called_once()
    finally:
        case.doCleanups()


async def probe(root):
    await startup_probe(root)
    case = RelayAdapterTests()
    await case.asyncSetUp()
    release, entered = threading.Event(), threading.Event()
    worker = None
    try:
        services = {
            "hermes_cli.middleware": SimpleNamespace(OBSERVER_SCHEMA_VERSION=1),
            "hermes_cli.plugins": SimpleNamespace(_resolve_hook_callback_timeout=lambda: 2,
                                                  resolve_plugin_command_result=lambda value: value),
        }
        with patch.dict(sys.modules, services), ThreadPoolExecutor(max_workers=2) as gateway_pool:
            dispatch = load_module(root, "hermes_cli/plugins_dispatch.py", "native_senler_hook_dispatch")
            threads = load_module(root, "tools/thread_context.py", "native_senler_thread_context")
            threads._callback_api = lambda: ()  # No terminal/vault prompt services are needed.
            manager = dispatch.PluginDispatchMixin()
            manager._hooks = {"pre_tool_call": [case.actions.before_tool],
                              "on_session_end": [case.actions.on_session_end]}
            manager._hook_timeout_lock = threading.Lock()
            manager._hook_timeout_suppressed_until = {}
            manager._hook_running_callbacks = {}
            manager._hook_abandoned = {}
            manager._hook_timeout_suppression_seconds = 60
            manager._hook_failures_reported = set()
            executor = native_method(root, "gateway/run.py", "GatewayRunner", "_run_in_executor_with_context")
            runner = SimpleNamespace(_get_executor=lambda: gateway_pool)
            loop = asyncio.get_running_loop()
            report_chat = case.id + ":new"

            def invoke(label):
                def call():
                    return manager.invoke_hook("pre_tool_call",
                        tool_name="mcp__senler_io_user__DialogsMessaging_sendMessage",
                        args={"id": "dialog-1", "content": label}, session_id=label, tool_call_id=label)
                with ThreadPoolExecutor(max_workers=1) as tool_pool:
                    result = tool_pool.submit(threads.propagate_context_to_thread(call)).result(timeout=3)
                manager.invoke_hook("on_session_end", session_id=label, task_id=label, turn_id=label,
                    platform="senler-io", completed=label != "new", failed=label == "new", interrupted=False)
                report = asyncio.run_coroutine_threadsafe(case.adapter.send(report_chat, label + " report", metadata={"notify": True}), loop).result(timeout=3)
                return result, report

            def delayed_turn():
                entered.set()
                if not release.wait(4):
                    raise TimeoutError("Delayed turn was not released")
                return invoke("old")

            old = await case.start_attempt("old")
            with case.actions.processing_attempt(case.adapter, old) as old_attempt:
                worker = asyncio.create_task(executor(runner, delayed_turn))
            try:
                assert await asyncio.to_thread(entered.wait, 2), "Gateway worker did not start"
                case.store.pause(case.id)
                case.store.resume(case.id, case.store.instructions(case.id)["local_revision"])
                new = await case.start_attempt("new")
                case.adapter.current = SimpleNamespace(source=SimpleNamespace(chat_id=report_chat))
                with case.actions.processing_attempt(case.adapter, new) as new_attempt:
                    current, saved = await executor(runner, invoke, "new")
                    assert current[0]["args"]["idempotency_key"] == "senler:" + case.id + ":new:reply", current
                    assert saved.success, "Report lost its context between the gateway worker and event loop"
                    release.set()
                    late, stale_report = await asyncio.wait_for(worker, 3)
                    assert late[0]["action"] == "block", late
                    assert not stale_report.success, "Late report borrowed the new event's context"
                    assert "not confirmed" in old_attempt.completion_error(), "Late callback revived the old attempt"
                    error = new_attempt.completion_error()
                    assert "processing failed" in error, "Completion lost its attempt context"
                ordinary, unbound_report = await executor(runner, invoke, "interactive")
                assert ordinary == [], "Scope leaked into a normal chat"
                assert not unbound_report.success, "An unbound report entered the active event"
                case.store.finish(case.id, new["event_id"], new["attempt_id"], error)
                with type(case.store)(case.temp.name).db() as db:
                    row = db.execute("SELECT result,failed FROM inbox WHERE event_id='new'").fetchone()
                assert row["failed"] == 1 and row["result"] == error + "\n\nPartial Hermes report:\nnew report", dict(row)
            finally:
                release.set()
                if worker:
                    await asyncio.gather(worker, return_exceptions=True)
    finally:
        release.set()
        case.doCleanups()
    print("Native Hermes startup and gateway/tool/hook context: restored turns rejected, inbox turns admitted; actions, completion and durable reports stay with their attempt; late workers blocked; normal chat unchanged")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    asyncio.run(probe(parser.parse_args().source_dir))
