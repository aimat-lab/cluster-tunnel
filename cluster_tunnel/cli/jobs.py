"""Job overview across clusters: jobs."""

from __future__ import annotations

import re

import rich_click as click

#: `--since` duration units expressed in minutes. A bare number is minutes.
_SINCE_UNITS = {"m": 1, "h": 60, "d": 60 * 24, "w": 60 * 24 * 7}
_SINCE_RE = re.compile(r"^\s*(\d+)\s*([mhdw]?)\s*$", re.IGNORECASE)


def _parse_since(text: str) -> int:
    """Parse a ``--since`` value like ``24h`` / ``90m`` / ``2d`` into minutes.

    A bare number is minutes; ``0`` (any unit) disables the finished-job window.
    Raises :class:`click.BadParameter` on anything else.
    """
    m = _SINCE_RE.match(text)
    if not m:
        raise click.BadParameter(
            f"invalid duration {text!r}; use e.g. 24h, 90m, 2d (or 0 for active only)."
        )
    value, unit = int(m.group(1)), m.group(2).lower()
    return value * _SINCE_UNITS.get(unit, 1)


def _fmt_since(minutes: int) -> str:
    """Render a minute count back as a compact duration for messages."""
    if minutes % (60 * 24) == 0:
        return f"{minutes // (60 * 24)}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _state_style(state: str) -> str:
    """Rich style for a Slurm state word (first token of the raw state).

    RUNNING (live) and COMPLETED (finished OK) get deliberately distinct hues —
    green vs. blue — so an active job never reads as a finished one at a glance.
    """
    head = state.split()[0].upper() if state else ""
    if head == "RUNNING":
        return "green"
    if head == "PENDING":
        return "yellow"
    if head == "COMPLETED":
        return "blue"
    if head in {"FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL"}:
        return "red"
    if head == "CANCELLED":
        return "bright_red"
    return "white"


class JobsCommandsMixin:
    """Mixin providing the `jobs` command."""

    @click.command("jobs")
    @click.pass_obj
    @click.option(
        "-s", "--since", "since", default="24h", metavar="DURATION",
        help="Finished-job history window (e.g. 24h, 90m, 2d; 0 = active only).",
    )
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def jobs_command(self, since: str, as_json: bool) -> None:
        """Show your Slurm jobs on the cluster(s): running, pending, recently finished.

        With a target (`-t <cluster>`), reports that cluster; without one, sweeps
        every configured cluster and reports each with a live tunnel (down ones
        are noted, never logged in to). For each cluster a bundled probe runs
        `squeue` (your running + pending jobs) and, unless `--since 0`, `sacct`
        (jobs that finished within the window) on the login node over the live
        tunnel — read-only, so it is never budget-guarded.
        """
        from cluster_tunnel import jobs as jobs_mod
        from cluster_tunnel import ssh

        since_minutes = _parse_since(since)

        config = self.load_config()
        if self.target:
            self.resolve_cluster(config)  # validate the target
            names = [self.target]
        else:
            names = sorted(config.clusters)

        results = []
        for name in names:
            cluster = config.clusters[name]
            spec = ssh.conn_spec(config, name)
            entry = {"cluster": name, "live": ssh.is_live(spec), "error": None, "jobs": []}
            if entry["live"]:
                try:
                    jobs = jobs_mod.query(spec, cluster.user, since_minutes)
                    entry["jobs"] = [j.as_dict() for j in jobs]
                except Exception as exc:  # noqa: BLE001 — surface, don't abort the sweep
                    entry["error"] = str(exc)
            results.append(entry)

        if as_json:
            import json

            click.echo(json.dumps(
                {"since_minutes": since_minutes, "clusters": results}, indent=2
            ))
            return

        self._render_jobs(results, since_minutes)

    def _render_jobs(self, results: list[dict], since_minutes: int) -> None:
        from rich.table import Table

        cons = self.cons

        if not results:
            click.echo("No clusters configured. Run `ctun config --init`.")
            return

        if since_minutes:
            cons.print(
                f"[dim]Your jobs — running, pending, and finished in the last "
                f"{_fmt_since(since_minutes)}.[/dim]\n"
            )
        else:
            cons.print("[dim]Your active jobs — running and pending.[/dim]\n")

        for r in results:
            name = r["cluster"]
            if not r["live"]:
                cons.print(
                    f"[bold cyan]{name}[/bold cyan]  "
                    f"[red]tunnel down[/red] [dim]— run `ctun -t {name} login`[/dim]\n"
                )
                continue
            if r["error"]:
                cons.print(
                    f"[bold cyan]{name}[/bold cyan]  "
                    f"[yellow]could not query jobs: {r['error']}[/yellow]\n"
                )
                continue

            jobs = r["jobs"]
            active = sum(1 for j in jobs if j["source"] == "active")
            done = len(jobs) - active
            counts = f"[dim]({active} active"
            counts += f", {done} finished)[/dim]" if since_minutes else ")[/dim]"

            if not jobs:
                cons.print(
                    f"[bold cyan]{name}[/bold cyan]  [dim]no jobs[/dim]\n"
                )
                continue

            table = Table(
                title=f"[bold cyan]{name}[/bold cyan]  {counts}",
                title_justify="left",
                show_header=True,
                header_style="bold",
                border_style="bright_black",
                expand=True,
            )
            table.add_column("Job ID", style="orange3", no_wrap=True)
            table.add_column("Name", overflow="ellipsis", max_width=32)
            table.add_column("State", no_wrap=True)
            table.add_column("Elapsed", no_wrap=True)
            table.add_column("Time limit", no_wrap=True)
            table.add_column("Partition", no_wrap=True)
            table.add_column("Nodes", justify="right", no_wrap=True)
            for j in jobs:
                state = j["state"].split()[0] if j["state"] else "—"
                table.add_row(
                    j["jobid"],
                    j["name"] or "—",
                    f"[{_state_style(j['state'])}]{state}[/]",
                    j["elapsed"] or "—",
                    j["timelimit"] or "—",
                    j["partition"] or "—",
                    j["nodes"] or "—",
                )
            cons.print(table)
            cons.print()
