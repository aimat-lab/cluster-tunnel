"""Command execution through the tunnel: run."""

from __future__ import annotations

import rich_click as click


class ExecutionCommandsMixin:
    """Mixin providing the `run` command."""

    @click.command("run", context_settings={"ignore_unknown_options": True})
    @click.pass_obj
    @click.option("--tty", is_flag=True, help="Allocate a pseudo-TTY (for interactive remote programs).")
    @click.option("-n", "--dry-run", is_flag=True, help="Run the budget pre-flight check only; don't execute.")
    @click.argument("command", nargs=-1, type=click.UNPROCESSED)
    def run_command(self, tty: bool, dry_run: bool, command: tuple[str, ...]) -> None:
        """Run a command on the cluster: ctun -t <cluster> run -- <cmd...>."""
        import shlex

        from cluster_tunnel import budget as budget_mod
        from cluster_tunnel import config as config_mod
        from cluster_tunnel import ssh

        if not command:
            raise click.UsageError("Provide a command after `--`, e.g. `run -- squeue --me`.")

        config = self.load_config()
        name, _ = self.resolve_cluster(config)
        spec = ssh.conn_spec(config, name)

        if not ssh.is_live(spec):
            raise click.ClickException(
                f"No live tunnel for '{name}'. Run `ctun -t {name} login` "
                f"(or `ctun -t {name} login --interactive`) first."
            )

        tokens = list(command)
        config_path = config_mod.resolve_config_path(self.config_path)
        decision = budget_mod.decide(config, name, spec, tokens, config_path)

        if dry_run:
            verdict = "ALLOW" if decision.allowed else "BLOCK"
            click.echo(f"[dry-run] {verdict} on {name}: {shlex.join(tokens)}")
            if decision.used is not None:
                click.echo(f"  budget: {decision.used} / {decision.limit} {decision.unit}")
            click.echo(f"  reason: {decision.reason}")
            return

        if not decision.allowed:
            raise click.ClickException(
                f"BLOCKED on {name}: {decision.reason}. Command not submitted."
            )

        raise SystemExit(ssh.run(spec, tokens, tty=tty))
