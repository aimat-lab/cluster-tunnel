"""Command execution through the tunnel: run."""

from __future__ import annotations

import rich_click as click

from cluster_tunnel.cli.errors import emit_marker, fail
from cluster_tunnel.constants import ExitCode


#: Fraction of the session budget at/above which `run` warns before the hard block.
_NEAR_LIMIT_FRACTION = 0.8


def _num(x: float) -> str:
    """Format a budget figure compactly (1 decimal, no trailing .0)."""
    return f"{round(x, 1):g}"


def _block_code(decision) -> ExitCode:
    """Classify a blocked budget decision into its exit code.

    A decision carrying a usage *number* (``used``/``limit`` both set) was blocked
    because the session is at/over budget; otherwise the budget could not be
    verified and was fail-closed.
    """
    if decision.used is not None and decision.limit is not None:
        return ExitCode.BUDGET_EXHAUSTED
    return ExitCode.BUDGET_GUARD_ERROR


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
        from cluster_tunnel import cmdlog
        from cluster_tunnel import config as config_mod
        from cluster_tunnel import ssh

        if not command:
            raise click.UsageError("Provide a command after `--`, e.g. `run -- squeue --me`.")

        config = self.load_config()
        name, _ = self.resolve_cluster(config)
        spec = ssh.conn_spec(config, name)

        if not ssh.is_live(spec):
            fail(
                f"No live tunnel for '{name}'. Run `ctun -t {name} login` "
                f"(or `ctun -t {name} login --interactive`) first.",
                ExitCode.LOGIN_REQUIRED,
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
            # Exit with the would-be failure code so `run -n` is a usable preflight.
            if not decision.allowed:
                code = _block_code(decision)
                emit_marker(code)
                raise SystemExit(int(code))
            return

        if not decision.allowed:
            # Budget exhausted (probe returned a number at/over the limit) gets a
            # specific, actionable message; other blocks (e.g. probe fail-closed)
            # fall back to the reason text.
            if decision.used is not None and decision.limit is not None:
                over = decision.used - decision.limit
                fail(
                    f"Submission blocked on '{name}': compute budget exhausted.\n"
                    f"  used {_num(decision.used)} / {_num(decision.limit)} {decision.unit} "
                    f"(over by {_num(over)} {decision.unit})\n"
                    f"  '{shlex.join(tokens)}' was NOT submitted.\n"
                    f"  Free budget by cancelling queued jobs, or start a fresh "
                    f"session with `ctun -t {name} login`.",
                    ExitCode.BUDGET_EXHAUSTED,
                )
            fail(
                f"Submission blocked on '{name}': {decision.reason}. Command not submitted.",
                ExitCode.BUDGET_GUARD_ERROR,
            )

        # When a probe actually ran (guarded command + a limit), show remaining
        # budget before the command's own output. Goes to stderr so it never
        # mixes into the command's stdout. Once usage crosses the near-limit
        # threshold, switch to a yellow warning so an agent can pace itself
        # before the hard block — but still let the command run.
        if decision.used is not None and decision.limit is not None:
            remaining = decision.limit - decision.used
            base = (
                f"{_num(decision.used)} / {_num(decision.limit)} {decision.unit} used"
                f" · {_num(remaining)} {decision.unit} remaining"
            )
            if decision.limit > 0 and decision.used / decision.limit >= _NEAR_LIMIT_FRACTION:
                pct = 100 * decision.used / decision.limit
                click.secho(f"budget: approaching limit — {pct:.0f}% used · {base}",
                            fg="yellow", err=True)
            else:
                click.secho(f"budget: {base}", fg="bright_black", err=True)

        # Log the command (not its output) so `status` can show activity.
        cmdlog.record(name, tokens)
        raise SystemExit(ssh.run(spec, tokens, tty=tty))
