"""Web interface command (placeholder): webui."""

from __future__ import annotations

import rich_click as click


class WebuiCommandsMixin:
    """Mixin providing the (future) `webui` command."""

    @click.command("webui")
    @click.pass_obj
    @click.option("--host", default="127.0.0.1", help="Bind host.")
    @click.option("-p", "--port", default=8765, type=int, help="Bind port.")
    @click.option("-n", "--no-browser", is_flag=True, help="Don't open a browser.")
    def webui_command(self, host: str, port: int, no_browser: bool) -> None:
        """Launch the local web interface (not yet implemented)."""
        click.echo("ctun webui is not yet implemented.")
