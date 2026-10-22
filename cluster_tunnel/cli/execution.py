"""Command execution through the tunnel: run."""

from __future__ import annotations

import rich_click as click

from cluster_tunnel.cli.errors import emit_marker, fail


#: Fraction of the session budget at/above which `run` warns before the hard block.
_NEAR_LIMIT_FRACTION = 0.8


def _num(x: float) -> str:
    """Format a budget figure compactly (1 decimal, no trailing .0)."""
    return f"{round(x, 1):g}"


def _echo_dry_run(name: str, tokens: list[str], decision) -> None:
    """Print the budget pre-flight verdict of `run -n`."""
    import shlex

    verdict = "ALLOW" if decision.allowed else "BLOCK"
    click.echo(f"[dry-run] {verdict} on {name}: {shlex.join(tokens)}")
    if decision.used is not None:
        click.echo(f"  budget: {decision.used} / {decision.limit} {decision.unit}")
    click.echo(f"  reason: {decision.reason}")


def _blocked_message(name: str, tokens: list[str], blocked) -> str:
    """Why a submission was blocked: the numbers when the budget is used up
    (with what to do about it), otherwise the guard's reason."""
    import shlex

    from cluster_tunnel import api

    decision = blocked.decision
    if isinstance(blocked, api.BudgetExhausted):
        over = decision.used - decision.limit
        return (
            f"Submission blocked on '{name}': compute budget exhausted.\n"
            f"  used {_num(decision.used)} / {_num(decision.limit)} {decision.unit} "
            f"(over by {_num(over)} {decision.unit})\n"
            f"  '{shlex.join(tokens)}' was NOT submitted.\n"
            f"  Free budget by cancelling queued jobs, or start a fresh "
            f"session with `ctun -t {name} login`."
        )
    return f"Submission blocked on '{name}': {decision.reason}. Command not submitted."


def _show_budget(decision) -> None:
    """When a probe actually ran (guarded command + a limit), show the remaining
    budget before the command's own output.

    Goes to stderr so it never mixes into the command's stdout. Once usage
    crosses the near-limit threshold, switch to a yellow warning so an agent can
    pace itself before the hard block — but still let the command run.
    """
    if decision.used is None or decision.limit is None:
        return
    remaining = decision.limit - decision.used
    base = (
        f"{_num(decision.used)} / {_num(decision.limit)} {decision.unit} used"
        f" · {_num(remaining)} {decision.unit} remaining"
    )
    if decision.limit > 0 and decision.used / decision.limit >= _NEAR_LIMIT_FRACTION:
        pct = 100 * decision.used / decision.limit
        click.secho(f"budget: approaching limit — {pct:.0f}% used · {base}", fg="yellow", err=True)
    else:
        click.secho(f"budget: {base}", fg="bright_black", err=True)


class ExecutionCommandsMixin:
    """Mixin providing the `run` command."""

    # ctun's options end at the command's first word: `run python train.py --note x`
    # passes `--note x` on to train.py, as written.
    @click.command("run", context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
    @click.pass_obj
    @click.option("--tty", is_flag=True, help="Allocate a pseudo-TTY (for interactive remote programs).")
    @click.option("-n", "--dry-run", is_flag=True, help="Run the budget pre-flight check only; don't execute.")
    @click.option("--project", default=None, help="Project name stored in the record of each job this command submits.")
    @click.option("--note", default=None, help="Note stored in the record of each job this command submits.")
    @click.argument("command", nargs=-1, type=click.UNPROCESSED)
    def run_command(
        self, tty: bool, dry_run: bool, project: str | None, note: str | None, command: tuple[str, ...]
    ) -> None:
        """Run a command on the cluster: ctun -t <cluster> run -- <cmd...>.

        For each `Submitted batch job <N>` in the output, ctun stores a job record
        on the cluster: `--project`, `--note`, and the facts it collects itself
        (commit, uncommitted files, working directory, ...). The command's output
        and exit code stay as they are.
        """
        from cluster_tunnel import api

        if not command:
            raise click.UsageError("Provide a command after `--`, e.g. `run -- squeue --me`.")

        config = self.load_config()
        name, _ = self.resolve_cluster(config)
        tokens = list(command)
        where = {"config": config, "config_path": self.config_path}

        try:
            if dry_run:
                _echo_dry_run(name, tokens, api.preflight(name, tokens, **where))
                return
            result = api.run(
                name, tokens, tty=tty, on_preflight=_show_budget, project=project, note=note, **where
            )
        except api.LoginRequired as exc:
            fail(str(exc), exc.exit_code)
        except api.NoteReferenceInvalid as exc:
            hint = (
                "\n  A job reference must name a job squeue, sacct or a job record knows."
                " Fix or drop it, or leave the note out."
                if exc.unknown
                else ""
            )
            fail(f"Not run: {exc}.{hint}", exc.exit_code)
        except api.BudgetBlocked as exc:
            if dry_run:
                # Exit with the would-be failure code so `run -n` is a usable preflight.
                _echo_dry_run(name, tokens, exc.decision)
                emit_marker(exc.exit_code)
                raise SystemExit(int(exc.exit_code)) from None
            fail(_blocked_message(name, tokens, exc), exc.exit_code)
        for warning in result.warnings:
            click.secho(f"ctun: {warning}", fg="yellow", err=True)
        for record in result.records:
            click.secho(f"ctun: recorded job {record.job_id}", fg="bright_black", err=True)
        raise SystemExit(result.exit_code)
