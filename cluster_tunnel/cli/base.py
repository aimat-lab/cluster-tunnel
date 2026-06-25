"""Base CLI class with custom Rich-formatted help output."""

from __future__ import annotations

import rich
import rich_click as click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from cluster_tunnel.cli.display import RichHelp, RichLogo


class BaseCLI(click.RichGroup):
    """Base CLI group providing Rich-formatted help output.

    Subclasses define a ``COMMAND_GROUPS`` class variable — a list of dicts with
    ``name`` and ``commands`` keys — controlling how commands are grouped in help.
    """

    COMMAND_GROUPS: list[dict] = []

    #: Set per-invocation by the root group callback from ``-t/--target``.
    target: str | None = None
    #: Set per-invocation by the root group callback from ``-c/--config``.
    config_path: str | None = None

    def __init__(self, *args, **kwargs):
        click.RichGroup.__init__(self, *args, invoke_without_command=True, **kwargs)
        self.cons = Console()

    def require_target(self) -> str:
        """Return the selected cluster, or raise a clean usage error."""
        if not self.target:
            raise click.UsageError(
                "No cluster selected — pass -t/--target <cluster> before the command, "
                "e.g. `ctun -t horeka run -- squeue --me`."
            )
        return self.target

    def load_config(self):
        """Load the config, turning a missing file into a clean usage error."""
        from cluster_tunnel import config as config_mod

        try:
            return config_mod.load_config(self.config_path)
        except FileNotFoundError as exc:
            raise click.UsageError(str(exc)) from exc

    def resolve_cluster(self, config):
        """Return (name, Cluster) for the selected target, or a usage error."""
        from cluster_tunnel import config as config_mod

        name = self.require_target()
        try:
            return name, config_mod.get_cluster(config, name)
        except KeyError as exc:
            raise click.UsageError(str(exc)) from exc

    def get_help(self, ctx):
        rich.print(RichLogo())
        rich.print(RichHelp())

        self.cons.print()
        self.cons.print(f" Usage: {ctx.command_path} [OPTIONS] COMMAND [ARGS]...")
        self.cons.print()

        options_table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        options_table.add_column("Option", style="cyan", min_width=20, max_width=20, no_wrap=True)
        options_table.add_column("Description", style="white", ratio=1)
        options_table.add_row("-t, --target TEXT", "Cluster to act on (from config).")
        options_table.add_row("-c, --config PATH", "Use an alternate config.yaml.")
        options_table.add_row("-V, --verbose", "Enable debug logging.")
        options_table.add_row("--version", "Show the version and exit.")
        options_table.add_row("--help", "Show this message and exit.")

        options_panel = Panel(
            options_table,
            title="[bold]Options[/bold]",
            title_align="left",
            border_style="bright_black",
            padding=(0, 1),
            expand=True,
        )
        self.cons.print(options_panel)
        self.cons.print()
        self._format_command_groups(ctx)
        return ""

    def _resolve_command(self, ctx, cmd_path: str):
        """Resolve a possibly nested command path like ``'config show'``."""
        parts = cmd_path.split()
        cmd = self.get_command(ctx, parts[0])
        for part in parts[1:]:
            if cmd is None or not isinstance(cmd, click.Group):
                return None
            cmd = cmd.get_command(ctx, part)
        return cmd

    def _format_command_groups(self, ctx) -> None:
        for group in self.COMMAND_GROUPS:
            table = Table(show_header=False, box=None, padding=(0, 1), expand=True)
            table.add_column("Command", style="cyan", min_width=20, max_width=20, no_wrap=True)
            table.add_column("Description", style="white", ratio=1)

            has_commands = False
            for cmd_name in group["commands"]:
                cmd = self._resolve_command(ctx, cmd_name)
                if cmd is not None:
                    table.add_row(cmd_name, cmd.get_short_help_str(limit=100))
                    has_commands = True

            if not has_commands:
                table.add_row("[dim]No commands yet[/dim]", "")

            panel = Panel(
                table,
                title=f"[bold]{group['name']}[/bold]",
                title_align="left",
                border_style="bright_black",
                padding=(0, 1),
                expand=True,
            )
            rich.print(panel)
