"""ctun CLI package.

Assembles command mixins into the final CLI group exposed as the ``ctun``
entry point. See PLAN.md / DESIGN.md for the overall design.
"""

from __future__ import annotations

import logging

import rich_click as click

from cluster_tunnel.constants import get_version
from cluster_tunnel.cli.base import BaseCLI
from cluster_tunnel.cli.configcmd import ConfigCommandsMixin
from cluster_tunnel.cli.context import ContextCommandsMixin
from cluster_tunnel.cli.execution import ExecutionCommandsMixin
from cluster_tunnel.cli.tunnel import TunnelCommandsMixin
from cluster_tunnel.cli.webui import WebuiCommandsMixin


class CLI(
    TunnelCommandsMixin,
    ExecutionCommandsMixin,
    ContextCommandsMixin,
    ConfigCommandsMixin,
    WebuiCommandsMixin,
    BaseCLI,
):
    """Main CLI class composing all command mixins."""

    COMMAND_GROUPS = [
        {"name": "Tunnel", "commands": ["login", "status", "logout"]},
        {"name": "Execution", "commands": ["run"]},
        {"name": "Miscellaneous", "commands": ["info", "logs", "config", "webui"]},
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_command(self.login_command)
        self.add_command(self.status_command)
        self.add_command(self.logout_command)
        self.add_command(self.run_command)
        self.add_command(self.info_command)
        self.add_command(self.logs_command)
        self.add_command(self.config_command)
        self.add_command(self.webui_command)


def _complete_clusters(ctx: click.Context, param, incomplete: str):
    """Tab-completion candidates for ``-t/--target``: the configured cluster names.

    Reads whichever config ``-c/--config``/``$CTUN_CONFIG`` resolves to, so the
    candidates always reflect the user's current clusters. Any error (no/invalid
    config) yields no candidates rather than breaking completion.
    """
    from click.shell_completion import CompletionItem

    from cluster_tunnel import config as config_mod

    try:
        config = config_mod.load_config(ctx.params.get("config_path"))
        names = list(config.clusters.keys())
    except Exception:  # noqa: BLE001 — completion must never raise
        return []
    return [CompletionItem(n) for n in names if n.startswith(incomplete)]


def _init_completion(ctx: click.Context, param, value):
    """Eager callback for ``--init-completion <shell>``: print the activation
    script for that shell and exit (mirrors ``--version``)."""
    if not value or ctx.resilient_parsing:
        return
    from click.shell_completion import get_completion_class

    comp_cls = get_completion_class(value)
    if comp_cls is None:  # Choice() should prevent this, but be defensive
        raise click.BadParameter(f"unsupported shell: {value}")
    comp = comp_cls(ctx.find_root().command, {}, "ctun", "_CTUN_COMPLETE")
    click.echo(comp.source())
    ctx.exit()


def create_cli():
    """Create and return the root CLI group."""

    @click.group(cls=CLI, context_settings={"show_default": True})
    @click.version_option(version=get_version(), message="%(version)s")
    @click.option(
        "--init-completion",
        type=click.Choice(["bash", "zsh", "fish"]),
        is_eager=True,
        expose_value=False,
        callback=_init_completion,
        help="Print a shell completion script for SHELL (bash|zsh|fish), then exit. "
        "e.g. `ctun --init-completion bash >> ~/.bashrc`.",
    )
    @click.option(
        "-t", "--target", default=None, help="Cluster to act on (from config).",
        shell_complete=_complete_clusters,
    )
    @click.option(
        "-c", "--config", "config_path", default=None, type=click.Path(),
        help="Use an alternate config.yaml.",
    )
    @click.option("-V", "--verbose", is_flag=True, help="Enable debug logging.")
    @click.pass_context
    def _cli(ctx: click.Context, target: str | None, config_path: str | None, verbose: bool) -> None:
        """cluster-tunnel — authenticated SSH tunnels + budget guard for HPC clusters."""
        if verbose:
            logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s: %(message)s")

        ctx.obj = ctx.command
        ctx.command.target = target
        ctx.command.config_path = config_path

        if ctx.invoked_subcommand is None:
            click.echo(ctx.get_help())

    return _cli


cli = create_cli()
