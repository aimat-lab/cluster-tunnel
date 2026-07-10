"""Agent-facing context command: info."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import rich_click as click
from rich.console import Group
from rich.panel import Panel
from rich.text import Text


class ContextCommandsMixin:
    """Mixin providing the `info` and `logs` commands."""

    @click.command("logs")
    @click.pass_obj
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def logs_command(self, as_json: bool) -> None:
        """Print the log of commands sent to a cluster (requires -t <cluster>).

        Only the commands are recorded — never their output. The log is
        temporary: it is cleared on `logout` and on a fresh `login`.
        """
        import shlex
        from datetime import datetime

        from cluster_tunnel import cmdlog

        config = self.load_config()
        name, _ = self.resolve_cluster(config)  # raises a usage error without -t

        records = cmdlog.entries(name)

        if as_json:
            import json

            click.echo(json.dumps({"cluster": name, "commands": records}, indent=2))
            return

        if not records:
            self.cons.print(f"No commands logged for [cyan]{name}[/cyan] this session.")
            return

        self.cons.print(f"[bold]Commands sent to [cyan]{name}[/cyan][/bold] ({len(records)} total)\n")
        for rec in records:
            ts = datetime.fromtimestamp(rec["epoch"]).strftime("%Y-%m-%d %H:%M:%S")
            cmd = shlex.join(rec.get("command", []))
            self.cons.print(f"  [dim]{ts}[/dim]  [orange3]{cmd}[/orange3]")

    @click.command("info")
    @click.pass_obj
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def info_command(self, as_json: bool) -> None:
        """Print the agent briefing: description, restrictions, and budget.

        With a target (`-t <cluster>`), shows that cluster. Without one, shows
        every configured cluster.
        """
        config = self.load_config()

        if self.target:
            from cluster_tunnel import config as config_mod

            try:
                config_mod.get_cluster(config, self.target)
            except KeyError as exc:
                raise click.UsageError(str(exc)) from exc
            names = [self.target]
        else:
            names = sorted(config.clusters)

        briefings = [self._gather_info(config, name) for name in names]

        if as_json:
            import json

            from cluster_tunnel.constants import get_version

            preamble = config.agent.preamble.strip()
            version = get_version()
            payloads = [self._info_payload(b) for b in briefings]
            if self.target:
                click.echo(json.dumps(
                    {"ctun_version": version, "preamble": preamble, **payloads[0]}, indent=2
                ))
            else:
                click.echo(json.dumps(
                    {"ctun_version": version, "preamble": preamble, "clusters": payloads}, indent=2
                ))
            return

        cons = self.cons
        if config.agent.preamble.strip():
            cons.print(f"[dim]{config.agent.preamble.strip()}[/dim]\n")

        if not briefings:
            cons.print("[yellow]No clusters configured.[/yellow]")
            return

        for briefing in briefings:
            cons.print(self._render_info(briefing))

    def _gather_info(self, config, name: str) -> dict[str, Any]:
        """Compute the live tunnel + budget state for a single cluster."""
        from cluster_tunnel import budget as budget_mod
        from cluster_tunnel import config as config_mod
        from cluster_tunnel import session, ssh

        cluster = config.clusters[name]
        spec = ssh.conn_spec(config, name)
        live = ssh.is_live(spec)
        sess = session.load(name)

        used: float | None = None
        usage_error: str | None = None
        limit = sess.get("limit") if sess else None
        unit = (sess.get("unit") if sess else None) or (
            cluster.budget.unit if cluster.budget else "units"
        )

        script_path: str | None = None
        if cluster.budget:
            config_path = config_mod.resolve_config_path(self.config_path)
            script_path = str(
                config_mod.budget_script_path(config_path, name, cluster.budget.script)
            )

        if live and sess and cluster.budget and limit is not None:
            try:
                user = budget_mod.remote_user(spec, cluster)
                used = budget_mod.used_since(
                    spec, Path(script_path), int(sess["start_epoch"]), user
                )
            except Exception as exc:  # noqa: BLE001
                usage_error = str(exc)

        return {
            "name": name,
            "cluster": cluster,
            "host": cluster.host,
            "ssh_alias": cluster.ssh_alias,
            "user": cluster.user,
            "requires_otp": cluster.requires_otp,
            "requires_password": cluster.requires_password,
            "live": live,
            "sess": sess,
            "used": used,
            "limit": limit,
            "unit": unit,
            "usage_error": usage_error,
            "script_path": script_path,
            "guard_commands": cluster.budget.guard_commands if cluster.budget else None,
            "fail_mode": cluster.budget.fail_mode if cluster.budget else None,
            "session_limit": cluster.budget.session_limit if cluster.budget else None,
        }

    @staticmethod
    def _info_payload(b: dict[str, Any]) -> dict[str, Any]:
        """Shape one cluster's briefing for JSON output."""
        cluster = b["cluster"]
        return {
            "cluster": b["name"],
            "description": cluster.description.strip(),
            "host": b["host"],
            "ssh_alias": b["ssh_alias"],
            "user": b["user"],
            "requires_otp": b["requires_otp"],
            "requires_password": b["requires_password"],
            "restrictions": cluster.restrictions,
            "tunnel_live": b["live"],
            "session": b["sess"],
            "budget": {
                "used": b["used"],
                "limit": b["limit"],
                "unit": b["unit"],
                "error": b["usage_error"],
                "script": b["script_path"],
                "guard_commands": b["guard_commands"],
                "fail_mode": b["fail_mode"],
                "session_limit": b["session_limit"],
            },
        }

    @staticmethod
    def _render_info(b: dict[str, Any]) -> Panel:
        """Render one cluster's briefing as a Rich panel."""
        cluster = b["cluster"]
        body: list[Any] = []

        if cluster.description.strip():
            body.append(Text(cluster.description.strip()))
            body.append("")

        body.append(Text("Connection", style="bold"))
        host = b["host"] or "[dim](via ssh config)[/dim]"
        body.append(f"  host: [orange3]{host}[/orange3]")
        if b["ssh_alias"]:
            body.append(f"  ssh alias: [orange3]{b['ssh_alias']}[/orange3]")
        user = b["user"] or "[dim](from ssh config)[/dim]"
        body.append(f"  user: [orange3]{user}[/orange3]")
        if b["requires_otp"]:
            otp = "[green]✓ true[/green]"
        else:
            otp = "[red]✗ false[/red]"
        body.append(f"  OTP required: {otp}")
        if b["requires_password"]:
            pw = "[green]✓ true[/green]"
        else:
            pw = "[red]✗ false[/red]"
        body.append(f"  Password required: {pw}")

        if cluster.restrictions:
            if body:
                body.append("")
            body.append(Text("Restrictions ", style="bold").append("(advisory)", style="dim"))
            for key, value in cluster.restrictions.items():
                body.append(f"  • {key}: {value}")

        if body:
            body.append("")
        body.append(Text("Budget", style="bold"))
        limit = b["limit"]
        if limit is None:
            body.append("  [dim]unguarded (no session limit set)[/dim]")
        else:
            used = b["used"]
            shown = f"{used}" if used is not None else "?"
            body.append(f"  used [orange3]{shown}[/orange3] / [orange3]{limit}[/orange3] {b['unit']}")
            if b["usage_error"]:
                body.append(f"  [yellow]usage unavailable: {b['usage_error']}[/yellow]")

        if b["guard_commands"]:
            guarded = ", ".join(b["guard_commands"])
            body.append(f"  guards: [orange3]{guarded}[/orange3]")
        if b["fail_mode"]:
            body.append(f"  on probe error: [orange3]{b['fail_mode']}[/orange3]")
        if b["script_path"]:
            body.append(f"  script: [orange3]{b['script_path']}[/orange3]")

        live = b["live"]
        status = "[green]live[/green]" if live else "[red]down[/red]"
        title = f"[bold cyan]{b['name']}[/bold cyan]"
        return Panel(
            Group(*body),
            title=title,
            title_align="left",
            subtitle=f"tunnel {status}",
            subtitle_align="right",
            border_style="bright_black",
            padding=(1, 2),
            expand=True,
        )
