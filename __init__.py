"""Native Hermes registrations; loading the plugin performs no connection or writes."""

from pathlib import Path

from .senler_connection import configure_parser, handle_command


def register(ctx):
    root = Path(__file__).parent
    skills = {
        "senler-io-user": "Connect Senler.io, work with projects and dialogs through User MCP, and set up webhook workflows.",
        "senler-io-incoming": "Process a supplied Senler.io webhook event according to the operator's configured workflow.",
    }
    for name, description in skills.items():
        ctx.register_skill(name, root / "skills" / name / "SKILL.md", description=description)
    ctx.register_cli_command(
        "senler",
        help="Connect Senler.io with the MCP settings bundled in this plugin",
        setup_fn=configure_parser,
        handler_fn=handle_command,
    )
