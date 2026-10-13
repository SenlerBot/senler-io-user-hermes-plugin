"""User-invoked setup. Connection credentials enter through JSON stdin only."""
import json
import os
import socket
import sys

from .senler_relay_store import PLATFORM, RelayStore, UNSET
from .senler_notifications import NotificationOutbox, resolve_target

ACTIONS = ["identity", "configure", "status", "instructions", "update-instructions", "pause", "resume", "remove",
           "resolve-notification-target", "test-notification", "notification-targets", "update-notification-target", "resolve-notification"]

RELAY_SCHEMA = {
    "name": "senler_relay",
    "description": (
        "Set up, inspect, pause or change Senler background event monitoring in this Hermes profile. "
        "Use for incoming messages, other webhook events and important-event notifications. "
        "Read the bundled senler-io-user skill and Relay setup reference first. "
        "This manages local settings; create/list/bind/test the Senler webhook through native MCP tools. "
        "Never write Python probes, read OAuth files or call private MCP APIs. "
        "Resolve a notification target BEFORE creating a monitoring subscription. "
        "A configured subscription or queued test is not proof of working processing/delivery."
    ),
    "parameters": {
        "type": "object", "additionalProperties": False, "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": ACTIONS},
            "enable_automation": {"type": "boolean", "description": "True only for background processing/resume requested by the operator."},
            "data": {"type": "object", "additionalProperties": False, "properties": {
                "connection": {"type": "object", "description": "The complete bind-Hermes response returned by Senler MCP."},
                "webhook_id": {"type": "string"},
                "instructions": {"type": "string", "maxLength": 8000},
                "expected_local_revision": {"type": ["integer", "null"]},
                "notification_target": {"type": ["string", "null"], "description": "origin or explicit platform:chat_id; omit to preserve, null to disable."},
                "target": {"type": "string", "description": "For resolve-notification-target: origin or explicit platform:chat_id."},
                "notification_id": {"type": "string"},
                "resolution": {"type": "string", "enum": ["delivered", "retry", "discard"],
                               "description": "Only after the operator checks an uncertain send and explicitly confirms delivery, requests a retry, or discards it."},
            }},
        },
    },
}


def gateway_block_reason(config):
    """Resolve the same profile-scoped proxy setting as the native gateway."""
    from agent.secret_scope import UnscopedSecretError, get_secret
    gateway = config.get("gateway") or {}
    if gateway.get("multiplex_profiles"):
        return "Use one gateway per Hermes profile for Senler Relay"
    try:
        proxy_url = (get_secret("GATEWAY_PROXY_URL") or "").strip()
    except UnscopedSecretError:
        proxy_url = os.environ.get("GATEWAY_PROXY_URL", "").strip()
    if proxy_url or (gateway.get("proxy_url") or "").strip():
        return ("Senler incoming events require the agent and plugin in the same Hermes instance. "
                "Gateway proxy mode (gateway.proxy_url / GATEWAY_PROXY_URL) is not supported. "
                "Run the plugin on the Hermes instance executing the agent; cloud model providers are supported.")
    return None


def require_supported_gateway(config):
    reason = gateway_block_reason(config)
    if reason:
        raise ValueError(reason)


def configure_parser(parser):
    parser.add_argument("relay_action", choices=ACTIONS)
    parser.add_argument("--enable-automation", action="store_true", help="Enable background processing authorized by the operator")


def run_action(action, data, *, enable_automation=False):
    from hermes_constants import get_hermes_home
    from hermes_cli.config import load_config_readonly, save_config
    from .senler_connection import NativeRuntime, connection_defaults, existing_connection
    if action not in ACTIONS or not isinstance(data, dict):
        raise ValueError("Invalid Senler Relay action or input")
    allowed = {
        "configure": {"connection", "instructions", "expected_local_revision", "notification_target"},
        "instructions": {"webhook_id"}, "update-instructions": {"webhook_id", "instructions", "expected_local_revision"},
        "pause": {"webhook_id"}, "resume": {"webhook_id", "expected_local_revision"}, "remove": {"webhook_id"},
        "resolve-notification-target": {"target"}, "test-notification": {"webhook_id"},
        "update-notification-target": {"webhook_id", "notification_target", "expected_local_revision"},
        "resolve-notification": {"webhook_id", "notification_id", "resolution"},
    }.get(action, set())
    if set(data) - allowed:
        raise ValueError("Unexpected relay input fields")
    if action == "resolve-notification-target":
        return {"target": resolve_target(data.get("target")), "delivery_verified": False}
    if action == "notification-targets":
        from tools.send_message_tool import send_message_tool
        return json.loads(send_message_tool({"action": "list"}))
    home = get_hermes_home()
    store = RelayStore(home)
    if action == "identity":
        return {"client_id": store.identity(), "client_name": socket.gethostname()[:100] or "Hermes"}
    if action == "status":
        return {"subscriptions": store.status(), "processing_blocked_reason": gateway_block_reason(load_config_readonly()),
                "notice": "Configured/enabled is not proof of a running gateway. Check connection, last_event and the exact notification test receipt separately."}
    if action == "configure":
        if not enable_automation:
            raise ValueError("Use --enable-automation only when the operator requested background processing")
        config = load_config_readonly()
        require_supported_gateway(config)
        if existing_connection(NativeRuntime(home, lambda: config), connection_defaults()) is None:
            raise ValueError("Connect Senler with senler_connect and native Hermes authorization first")
        target = data.get("notification_target", UNSET)
        if target is not UNSET and target is not None:
            target = resolve_target(target)
        store.configure(data["connection"], data.get("instructions"), data.get("expected_local_revision"), target)
        was_enabled = config.get("platforms", {}).get(PLATFORM, {}).get("enabled") is True
        try:
            save_config({"platforms": {PLATFORM: {"enabled": True}}}, merge_existing=True)
            if load_config_readonly().get("platforms", {}).get(PLATFORM, {}).get("enabled") is not True:
                raise ValueError("Hermes did not save the Senler platform setting")
        except Exception:
            store.pause(data["connection"]["webhook_id"])
            raise
        return {"configured": True, "webhook_id": data["connection"]["webhook_id"],
                "gateway_restart_required": not was_enabled, "processing_verified": False,
                "notification_delivery_verified": False}
    key = data.get("webhook_id")
    if not isinstance(key, str) or key not in store.subscriptions():
        raise ValueError("Unknown local Senler subscription")
    if action == "test-notification":
        return {"notification_test_id": NotificationOutbox(store).test(key), "status": "queued",
                "notice": "Wait for notifications.last_test with this exact ID and status=delivered. No model is invoked."}
    if action == "resolve-notification":
        NotificationOutbox(store).resolve(key, data.get("notification_id"), data.get("resolution"))
        return {"notification_id": data["notification_id"], "resolution": data["resolution"],
                "notice": "A retry queues the saved text and recipient; it does not reprocess the event."}
    if action == "update-notification-target":
        if "notification_target" not in data:
            raise ValueError("Pass notification_target explicitly; null disables notifications")
        target = resolve_target(data["notification_target"]) if data["notification_target"] is not None else None
        store.update_notification_target(key, target, data.get("expected_local_revision"))
        return store.instructions(key)
    if action in {"instructions", "update-instructions"}:
        if action == "update-instructions":
            store.update_instructions(key, data.get("instructions"), data.get("expected_local_revision"))
        return store.instructions(key)
    if action == "resume":
        if not enable_automation:
            raise ValueError("Use --enable-automation for an operator-authorized resume")
        require_supported_gateway(load_config_readonly())
        store.resume(key, data.get("expected_local_revision"))
    else:
        getattr(store, action)(key)
    return {"webhook_id": key, "local_action": action, "update_senler_via_mcp": True}


def manage_relay(args, **kwargs):
    try:
        from tools.connectors.turn import CARD, SIDE, connection_surface
        from gateway.session_context import get_session_env, session_is_messaging_surface
        from .senler_relay_actions import _attempt
        if (connection_surface() == SIDE or _attempt.get() is not None
                or get_session_env("HERMES_CRON_SESSION", "")
                or os.environ.get("HERMES_KANBAN_TASK")
                or (connection_surface() != CARD and not session_is_messaging_surface())):
            raise ValueError("Manage Senler subscriptions from the operator's interactive chat")
        if not isinstance(args, dict) or set(args) - {"action", "data", "enable_automation"}:
            raise ValueError("Invalid senler_relay arguments")
        if type(args.get("enable_automation", False)) is not bool:
            raise ValueError("enable_automation must be boolean")
        return json.dumps(run_action(args.get("action"), args.get("data", {}),
                                    enable_automation=args.get("enable_automation", False)), ensure_ascii=False)
    except ValueError as error:
        return json.dumps({"error": str(error)}, ensure_ascii=False)
    except Exception:
        return json.dumps({"error": "Could not complete local Senler setup. Check Hermes compatibility and the active profile; no credentials were printed."})


def handle_command(args):
    try:
        data = {}
        if args.relay_action not in {"identity", "status", "notification-targets"}:
            if sys.stdin.isatty():
                raise ValueError("Pass the relay configuration as JSON stdin")
            data = json.loads(sys.stdin.read(65537))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object on stdin")
        result = run_action(args.relay_action, data, enable_automation=args.enable_automation)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (KeyError, OSError, ValueError) as error:
        # Invalid JSON/configuration may contain a secret; report only controlled messages.
        message = str(error) if type(error) is ValueError else "Invalid relay input or local settings; no credentials were printed"
        print("Senler: " + message, file=sys.stderr)
        return 1
