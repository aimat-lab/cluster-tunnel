"""Rich display classes for the ctun CLI."""

from __future__ import annotations

from pathlib import Path

from rich.padding import Padding
from rich.style import Style
from rich.text import Text

ASSETS_DIR = Path(__file__).parent / "assets"


class RichLogo:
    """Renders the ctun ASCII logo."""

    STYLE = Style(bold=True, color="cyan")

    def __rich_console__(self, console, options):
        text_path = ASSETS_DIR / "logo.txt"
        try:
            text_string = text_path.read_text()
        except FileNotFoundError:
            text_string = "ctun"
        yield Padding(Text(text_string, style=self.STYLE), (1, 3, 0, 3))


class RichHelp:
    """Renders the project description below the logo."""

    def __rich_console__(self, console, options):
        yield (
            "[cyan bold]cluster-tunnel[/cyan bold] (ctun) — authenticated SSH tunnels "
            "+ budget guard for HPC clusters"
        )
        yield ""
        yield (
            "Authenticate once (OTP) and keep an SSH ControlMaster tunnel alive so coding "
            "agents can funnel commands to a cluster without re-authenticating — with a "
            "session-scoped compute-budget guard that hard-blocks overspend."
        )
