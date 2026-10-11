"""Prepare the bundled MCP for Hermes's native in-chat authorization card."""

import json

from .senler_connection import NativeRuntime, connection_defaults, existing_connection


CONNECT_SCHEMA = {
    "name": "senler_connect",
    "description": (
        "Prepare Senler.io for connection in the user's interactive Hermes chat. "
        "Use when the user asks to connect Senler or use Senler and no working connection exists. "
        "The MCP address and OAuth settings are bundled; this takes no credentials or URL. "
        "It only prepares settings, NOT authorization. Then call the returned manage_connections "
        "actions to show Hermes's native authorization card. Never use from a webhook, "
        "scheduled task or subagent. Reuses existing compatible settings."
    ),
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


class SetupError(ValueError):
    """A fixed, credential-free explanation safe to return to the chat."""


def _existing(runtime, defaults):
    try:
        return existing_connection(runtime, defaults, allow_disabled=True)
    except ValueError:
        raise SetupError("The existing senler-io-user MCP configuration is incompatible. "
                         "Review it in Hermes MCP settings; it was preserved.") from None


def _prepare(config):
    defaults = connection_defaults()
    name = defaults["name"]
    # Use the same lock and writer as Hermes. Resolve the home at call time:
    # Desktop can switch profiles after the plugin was registered.
    with config._CONFIG_LOCK:
        raw = config.require_readable_config_before_write()
        runtime = NativeRuntime("", config.load_config_readonly)
        existing = _existing(runtime, defaults)
        created = existing is None
        if created:
            if config.is_managed():
                raise SetupError("This Hermes profile has managed settings; add Senler through its administrator.")
            servers = raw.setdefault("mcp_servers", {})
            if not isinstance(servers, dict):
                raise SetupError("Hermes mcp_servers is not a mapping; repair it in Hermes settings first.")
            if name in servers:
                raise SetupError("The saved Senler settings differ from Hermes's loaded configuration. "
                                 "Reload this profile and retry; the saved entry was preserved.")
            servers[name] = {key: value for key, value in defaults.items() if key != "name"}
            config.save_config(raw)
            existing = _existing(runtime, defaults)
            if existing is None or existing.get("enabled", True) is not True:
                raise SetupError("Hermes did not save an enabled Senler connection; check this profile's settings.")

    actions = ["authorize"]
    if existing.get("enabled", True) is False:
        actions.insert(0, "enable")
    return {
        "status": "authorization_pending",
        "server": name,
        "created": created,
        "next_calls": [
            {"tool": "manage_connections",
             "arguments": {"action": action, "connectors": [{"name": name, "mcp": True}]}}
            for action in actions
        ],
        "message": (
            "Settings are ready; authorization and tool discovery have NOT been verified. "
            "Call next_calls in order, stopping on skip, cancellation, error or timeout. "
            "Hermes reuses valid saved authorization. Only report connected after Hermes "
            "confirms it; discovery_error means tools are still unavailable. "
            "If manage_connections is unavailable in this session, ask the user to open "
            "Hermes Desktop connections/MCP, select senler-io-user, enable it if disabled, "
            "and click Authorize. Its address is already filled in."
        ),
    }


def prepare_connection(args, **kwargs):
    if not isinstance(args, dict) or args:
        return json.dumps({"error": "senler_connect takes no arguments; connection settings are bundled."})
    try:
        from tools.connectors.turn import CARD, connection_surface
        from .senler_relay_actions import _attempt

        if connection_surface() != CARD or _attempt.get() is not None:
            return json.dumps({"error": "Open an interactive Hermes chat and ask to connect Senler there."})
        from hermes_cli import config

        # Don't swallow older API gaps and fall back to editing YAML ourselves.
        required = ("_CONFIG_LOCK", "require_readable_config_before_write", "load_config_readonly",
                    "save_config", "is_managed")
        if not all(hasattr(config, name) for name in required):
            return json.dumps({"error": "Update Hermes to use Senler's in-chat connection setup."})
        return json.dumps(_prepare(config), ensure_ascii=False)
    except ImportError:
        return json.dumps({"error": "Update Hermes to use Senler's in-chat connection setup."})
    except SetupError as exc:
        return json.dumps({"error": str(exc)})
    except Exception:
        # A config parser/write error can include secrets from unrelated YAML.
        # Keep raw exceptions and settings out of the model transcript.
        return json.dumps({"error": "Could not prepare Senler. Check the active Hermes profile's MCP settings "
                                    "for an existing conflicting server, invalid configuration or a write restriction. "
                                    "No authorization was performed."})
