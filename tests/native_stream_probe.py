"""Verify report delivery with pinned Hermes streaming selection and consumer.

No model, network, OAuth or customer traffic. The unchanged native methods
select the delivery mode; completed messages go through the real Senler inbox.
"""
import argparse
import asyncio
from contextlib import suppress
import json
from pathlib import Path
import sys
from types import MethodType, ModuleType, SimpleNamespace
from unittest.mock import patch

from native_hook_probe import load_module, native_method
from test_relay_adapter import FakeGatewayBase, RelayAdapterTests


async def probe(root):
    case = RelayAdapterTests()
    await case.asyncSetUp()
    try:
        modules = {
            "gateway.platforms.base": SimpleNamespace(BasePlatformAdapter=FakeGatewayBase,
                _custom_unit_to_cp=lambda text, budget, len_fn: min(len(text), budget)),
            "gateway.config": SimpleNamespace(DEFAULT_STREAMING_EDIT_INTERVAL=0.01,
                DEFAULT_STREAMING_BUFFER_THRESHOLD=1, DEFAULT_STREAMING_CURSOR="▉"),
            "gateway.platforms.event": SimpleNamespace(MessageEvent=SimpleNamespace),
            "utils": SimpleNamespace(atomic_json_write=lambda *args, **kwargs: None),
            "hermes_cli.observability.shared_metrics_gateway": SimpleNamespace(stops_reply_clock=lambda fn: fn),
        }
        for name in ["gateway", "gateway.platforms", "gateway.relay", "agent", "hermes_cli", "hermes_cli.observability"]:
            package = ModuleType(name)
            package.__path__ = []
            modules[name] = package
        with patch.dict(sys.modules, modules):
            for path, name in [
                ("agent/think_scrubber.py", "agent.think_scrubber"),
                ("gateway/platforms/helpers.py", "gateway.platforms.helpers"),
                ("gateway/relay/egress.py", "gateway.relay.egress"),
            ]:
                load_module(root, path, name)
            for name in ["response_filters", "stream_consumer_fences", "stream_consumer_transport",
                         "stream_consumer_fallback", "stream_consumer_think", "stream_consumer"]:
                load_module(root, "gateway/" + name + ".py", "gateway." + name)
            build = native_method(root, "gateway/run_turn.py", "GatewayTurnMixin", "_build_stream_consumer_config")
            setup = native_method(root, "gateway/run_turn_runner.py", "TurnRunner", "_setup_stream_consumer")
            mark_delivered = native_method(root, "gateway/run_turn.py", "GatewayTurnMixin", "_run_agent_mark_streamed_delivery")
            confirm_delivery = native_method(root, "gateway/run_turn.py", "GatewayTurnMixin", "_run_agent_stream_confirmed_final_delivery").__func__
            final_metadata = native_method(root, "gateway/platforms/base.py", None, "_mark_notify_metadata")
            notice = native_method(root, "gateway/run_notifications.py", "GatewayNotificationsMixin", "_deliver_platform_notice")
            notice.__globals__["Platform"].SLACK = "slack"
            sys.modules["gateway.run"] = SimpleNamespace(_is_slack_ignored_channel=lambda *args: False)
            mark_delivered.__globals__["suppress"] = suppress
            confirm_delivery.__globals__["suppress"] = suppress
            case.adapter.message_len_fn = len
            case.adapter.truncate_message = lambda text, limit, **kwargs: [text]
            scfg = SimpleNamespace(enabled_for=lambda override: True, cursor="▉", edit_interval=0.01,
                                   buffer_threshold=1, transport="edit")
            runner = SimpleNamespace(config=SimpleNamespace(streaming=scfg), _delivery_adapter_for=lambda source: case.adapter)
            runner._build_stream_consumer_config = MethodType(build, runner)
            runner._run_agent_stream_confirmed_final_delivery = confirm_delivery
            runner._thread_metadata_for_source = lambda source: None
            with patch.object(FakeGatewayBase, "edit_message", native_method(root, "gateway/platforms/base.py",
                              "BasePlatformAdapter", "edit_message"), create=True), \
                    patch.object(FakeGatewayBase, "delete_message", native_method(root, "gateway/platforms/base.py",
                                 "BasePlatformAdapter", "delete_message"), create=True), \
                    patch.object(FakeGatewayBase, "strip_media_directives_for_display", staticmethod(lambda text: text), create=True):
                quiet = '{"notify":false,"summary":"Ordinary greeting"}'
                attention = '{"notify":true,"summary":"Needs attention"}'
                cases = [("Hello world", None, False), ("Hello world", "Checked.", False),
                         ("Hello world", "Hello world", False), ("Hello world", "Checked.", True),
                         (quiet, None, False), (quiet, "Checked.", False), (quiet, quiet, False),
                         (attention, attention, False)]
                for index, (final_text, comment, interrupted) in enumerate(cases):
                    if index == 4:
                        case.store.configure(case.connection, expected_local_revision=0,
                            notification_target={"platform": "telegram", "chat_id": "operator", "thread_id": None})
                        case.config = case.store.subscriptions()[case.id]
                    interim = comment is not None
                    event_id = "stream-" + str(index)
                    row = await case.start_attempt(event_id)
                    source = SimpleNamespace(platform="senler-io", chat_id=case.id + ":" + event_id, chat_type="dm")
                    case.adapter.current = SimpleNamespace(source=source)
                    ctx = SimpleNamespace(mute_notification_reply=False, streaming_tts_consumer_holder=[None],
                        resolve_display_setting=lambda *args: None, user_config={}, scheduled_heartbeat=False,
                        interim_assistant_messages_enabled=interim, source=source, _status_thread_metadata=None,
                        progress_queue=None, event_message_id=event_id, _run_still_current=lambda: True,
                        stream_consumer_holder=[None], _status_adapter=None, session_key=event_id)
                    with case.actions.processing_attempt(case.adapter, row):
                        # The same native path that sent /sethome in the failed event.
                        await notice(runner, source, "📬 No home channel is set for Senler-Io. Type /sethome.")
                        consumer, deltas, commentary, _ = setup(SimpleNamespace(_ctx=ctx, _runner=runner), "senler-io")
                        assert deltas is None, "Native gateway would feed partial tokens into the durable report"
                        if consumer:
                            assert not consumer.stream_deltas_enabled and consumer.cfg.cursor == ""
                            task = asyncio.create_task(consumer.run())
                            try:
                                commentary(comment)
                                consumer.finish(None if interrupted else final_text)
                                await asyncio.wait_for(task, 2)
                            finally:
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                        else:
                            assert not interim
                        if not interrupted:
                            response = {"final_response": final_text}
                            await mark_delivered(runner, response, ctx)
                            # Native final-send selection decides whether the
                            # complete reply still needs the normal send path.
                            if not response.get("already_sent"):
                                assert (await case.adapter.send(source.chat_id, response["final_response"],
                                                               metadata=final_metadata(None))).success
                        await notice(runner, source, "Runtime footer")
                    case.store.finish(case.id, row["event_id"], row["attempt_id"], "Interrupted" if interrupted else None)
                    with case.store.db() as db:
                        saved = db.execute("SELECT result,failed FROM inbox WHERE event_id=?", (event_id,)).fetchone()
                    if interrupted:
                        assert saved["failed"] == 1 and saved["result"] == "Interrupted", dict(saved)
                    else:
                        assert saved["failed"] == 0 and saved["result"] == final_text, dict(saved)
                    if index >= 4:
                        with case.store.db() as db:
                            notifications = db.execute("SELECT content FROM notification_queue WHERE event_id=?", (event_id,)).fetchall()
                        assert len(notifications) == int(json.loads(final_text)["notify"]), notifications
            assert scfg.cursor == "▉", "The plugin must not rewrite global streaming settings"
    finally:
        await case.asyncTearDown()
        case.doCleanups()
    print("Native Hermes notices and streaming: only final text enters Senler reports; identical interim text cannot suppress the final; notification decisions stay valid; interrupted turns stay failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    asyncio.run(probe(parser.parse_args().source_dir))
