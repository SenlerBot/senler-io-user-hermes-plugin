"""User-invoked setup. Connection credentials enter through JSON stdin only."""
import json
import os
import socket
import sys

from .senler_relay_store import PLATFORM, RelayStore


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
    parser.add_argument("relay_action", choices=["identity", "configure", "status", "instructions", "update-instructions", "pause", "resume", "remove"])
    parser.add_argument("--enable-automation", action="store_true", help="Enable background processing authorized by the operator")


def handle_command(args):
    from hermes_constants import get_hermes_home
    from hermes_cli.config import load_config_readonly, save_config
    from .senler_connection import NativeRuntime, connection_defaults, existing_connection
    try:
        home = get_hermes_home()
        store = RelayStore(home)
        if args.relay_action == "identity":
            result = {"client_id": store.identity(), "client_name": socket.gethostname()[:100] or "Hermes"}
        elif args.relay_action == "status":
            result = {"subscriptions": store.status(),
                      "processing_blocked_reason": gateway_block_reason(load_config_readonly())}
        else:
            if sys.stdin.isatty():
                raise ValueError("Pass the relay configuration as JSON stdin")
            data = json.loads(sys.stdin.read(65537))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object on stdin")
            if args.relay_action == "configure":
                if not args.enable_automation:
                    raise ValueError("Use --enable-automation only when the operator requested background processing")
                config = load_config_readonly()
                require_supported_gateway(config)
                if existing_connection(NativeRuntime(home, lambda: config), connection_defaults()) is None:
                    raise ValueError("Run hermes senler connect first")
                if set(data) - {"connection", "instructions", "expected_local_revision"}:
                    raise ValueError("Unexpected relay configuration fields")
                store.configure(data["connection"], data.get("instructions"), data.get("expected_local_revision"))
                was_enabled = config.get("platforms", {}).get(PLATFORM, {}).get("enabled") is True
                save_config({"platforms": {PLATFORM: {"enabled": True}}}, merge_existing=True)
                if load_config_readonly().get("platforms", {}).get(PLATFORM, {}).get("enabled") is not True:
                    store.pause(data["connection"]["webhook_id"])
                    raise ValueError("Hermes did not save the Senler platform setting")
                result = {"configured": True, "webhook_id": data["connection"]["webhook_id"], "gateway_restart_required": not was_enabled}
            else:
                key = data.get("webhook_id")
                if not isinstance(key, str) or key not in store.subscriptions():
                    raise ValueError("Unknown local Senler subscription")
                if args.relay_action in {"instructions", "update-instructions"}:
                    allowed_fields = {"webhook_id"} if args.relay_action == "instructions" else {
                        "webhook_id", "instructions", "expected_local_revision"}
                    if set(data) - allowed_fields:
                        raise ValueError("Unexpected instruction fields")
                    if args.relay_action == "update-instructions":
                        store.update_instructions(key, data.get("instructions"), data.get("expected_local_revision"))
                    result = store.instructions(key)
                elif args.relay_action == "resume":
                    if not args.enable_automation:
                        raise ValueError("Use --enable-automation for an operator-authorized resume")
                    require_supported_gateway(load_config_readonly())
                    store.resume(key, data.get("expected_local_revision"))
                else:
                    getattr(store, args.relay_action)(key)
                if args.relay_action not in {"instructions", "update-instructions"}:
                    result = {"webhook_id": key, "local_action": args.relay_action, "update_senler_via_mcp": True}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (KeyError, OSError, ValueError) as error:
        # Invalid JSON/configuration may contain a secret; report only controlled messages.
        message = str(error) if type(error) is ValueError else "Invalid relay input or local settings; no credentials were printed"
        print("Senler: " + message, file=sys.stderr)
        return 1
