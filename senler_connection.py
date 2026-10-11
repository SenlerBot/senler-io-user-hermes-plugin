"""Package-owned connection defaults, handed to Hermes's own MCP CLI for OAuth."""

import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit


def interactive_console():
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return False
        if sys.platform != "win32":
            return True
        # Windows reports isatty() for some non-console character devices,
        # including NUL. Reauthorization must require a real input console.
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        mode = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetConsoleMode(
            ctypes.c_void_p(handle), ctypes.byref(mode),
        ))
    except (AttributeError, OSError, ValueError):
        return False


def connection_defaults():
    config = json.loads(Path(__file__).with_name("mcp-server.json").read_text(encoding="utf-8"))
    if set(config) != {"name", "url", "auth", "connect_timeout"}:
        raise ValueError("Invalid bundled Senler MCP configuration")
    url = urlsplit(config["url"])
    if (config["name"] != "senler-io-user" or config["auth"] != "oauth"
            or url.scheme != "https" or not url.hostname or url.username
            or url.password or url.fragment or config["connect_timeout"] != 300):
        raise ValueError("Invalid bundled Senler MCP configuration")
    return config


class NativeRuntime:
    def __init__(self, home, read_config):
        self.home = str(home)
        self.read_config = read_config

    def servers(self):
        config = self.read_config()
        if not isinstance(config, dict):
            raise ValueError("Hermes configuration is not a mapping")
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("Hermes mcp_servers is not a mapping")
        return servers

    def interactive(self):
        return interactive_console()

    def invoke(self, arguments):
        # Same interpreter and resolved profile as the parent Hermes invocation.
        # The public CLI owns prompts, token storage, discovery and config writes.
        env = {**os.environ, "HERMES_HOME": self.home}
        return subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "mcp", *arguments],
            env=env, check=False,
        ).returncode


def existing_connection(runtime, defaults):
    servers = runtime.servers()
    if defaults["name"] not in servers:
        return None
    existing = servers[defaults["name"]]
    if (not isinstance(existing, dict) or existing.get("url") != defaults["url"]
            or existing.get("auth") != defaults["auth"] or "command" in existing):
        raise ValueError(
            "The name senler-io-user already belongs to a different MCP configuration. "
            "Existing settings were preserved; resolve the conflict in Hermes first."
        )
    if existing.get("enabled", True) is not True:
        raise ValueError(
            "The Senler MCP connection is disabled. Enable it in Hermes MCP settings first; "
            "the plugin did not replace its settings."
        )
    return existing


def connect(runtime, *, reauthorize=False):
    defaults = connection_defaults()
    existing = existing_connection(runtime, defaults)
    if not runtime.interactive():
        raise ValueError(
            "Open PowerShell or an interactive terminal for this Hermes profile and run "
            "'hermes senler connect'" + (" with --reauthorize" if reauthorize else "")
            + ". Sign-in requires the user's participation."
        )

    if existing is None:
        print("Connecting Senler using the settings bundled in the plugin. Complete sign-in in Hermes.")
        result = runtime.invoke([
            "add", defaults["name"], "--url", defaults["url"],
            "--auth", defaults["auth"], "--connect-timeout", str(defaults["connect_timeout"]),
        ])
        if result:
            return result
        # `mcp add` can exit zero after cancellation or failed discovery. A saved,
        # enabled connection is required before any success can be reported.
        if existing_connection(runtime, defaults) is None:
            raise ValueError("Senler connection was not saved. Complete sign-in and run 'hermes senler connect' again.")
    elif reauthorize:
        result = runtime.invoke(["login", defaults["name"]])
        if result:
            return result

    result = runtime.invoke(["test", defaults["name"]])
    if result == 0:
        print("Senler MCP connection verified. Open a new Hermes session or run /reload-mcp.")
    else:
        print("Senler connection could not be verified. If sign-in expired, run 'hermes senler connect --reauthorize'.")
    return result


def check(runtime):
    defaults = connection_defaults()
    if existing_connection(runtime, defaults) is None:
        raise ValueError("Senler is not connected yet. Run 'hermes senler connect'.")
    return runtime.invoke(["test", defaults["name"]])


def configure_parser(parser):
    actions = parser.add_subparsers(dest="senler_action", required=True)
    setup = actions.add_parser("connect", help="Sign in to Senler using the bundled MCP connection")
    setup.add_argument("--reauthorize", action="store_true", help="Start a fresh Senler OAuth sign-in")
    actions.add_parser("check", help="Check the existing Senler MCP connection")
    from .senler_relay_setup import configure_parser as relay_parser
    relay_parser(actions.add_parser("relay", help="Configure outgoing Senler event connections"))


def handle_command(args):
    from hermes_constants import get_hermes_home
    from hermes_cli.config import load_config_readonly

    runtime = NativeRuntime(get_hermes_home(), load_config_readonly)
    try:
        if args.senler_action == "relay":
            from .senler_relay_setup import handle_command as relay_command
            return relay_command(args)
        if args.senler_action == "check":
            return check(runtime)
        return connect(runtime, reauthorize=args.reauthorize)
    except (OSError, ValueError) as exc:
        print(f"Senler: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Senler sign-in interrupted.", file=sys.stderr)
        return 130
