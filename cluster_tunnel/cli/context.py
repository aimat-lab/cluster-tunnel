"""Agent-facing context command: info."""

from __future__ import annotations

import rich_click as click


class ContextCommandsMixin:
    """Mixin providing the `info` briefing command."""

    @click.command("info")
    @click.pass_obj
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def info_command(self, as_json: bool) -> None:
        """Print the agent briefing: description, restrictions, and budget."""
        from cluster_tunnel import budget as budget_mod
        from cluster_tunnel import config as config_mod
        from cluster_tunnel import session, ssh

        config = self.load_config()
        name, cluster = self.resolve_cluster(config)
        spec = ssh.conn_spec(config, name)
        live = ssh.is_live(spec)
        sess = session.load(name)

        used: float | None = None
        usage_error: str | None = None
        limit = sess.get("limit") if sess else None
        unit = (sess.get("unit") if sess else None) or (cluster.budget.unit if cluster.budget else "units")

        if live and sess and cluster.budget and limit is not None:
            try:
                config_path = config_mod.resolve_config_path(self.config_path)
                script = config_mod.budget_script_path(config_path, name, cluster.budget.script)
                user = budget_mod.remote_user(spec, cluster)
                used = budget_mod.used_since(spec, script, int(sess["start_epoch"]), user)
            except Exception as exc:  # noqa: BLE001
                usage_error = str(exc)

        if as_json:
            import json

            click.echo(
                json.dumps(
                    {
                        "cluster": name,
                        "preamble": config.agent.preamble.strip(),
                        "description": cluster.description.strip(),
                        "restrictions": cluster.restrictions,
                        "tunnel_live": live,
                        "session": sess,
                        "budget": {
                            "used": used,
                            "limit": limit,
                            "unit": unit,
                            "error": usage_error,
                        },
                    },
                    indent=2,
                )
            )
            return

        cons = self.cons
        cons.print(f"[bold cyan]{name}[/bold cyan]  —  tunnel "
                   + ("[green]live[/green]" if live else "[red]down[/red]"))
        if config.agent.preamble.strip():
            cons.print(f"\n[dim]{config.agent.preamble.strip()}[/dim]")
        if cluster.description.strip():
            cons.print(f"\n{cluster.description.strip()}")

        if cluster.restrictions:
            cons.print("\n[bold]Restrictions[/bold] [dim](advisory)[/dim]")
            for key, value in cluster.restrictions.items():
                cons.print(f"  • {key}: {value}")

        cons.print("\n[bold]Budget[/bold]")
        if limit is None:
            cons.print("  unguarded (no session limit set)")
        else:
            shown = f"{used}" if used is not None else "?"
            cons.print(f"  used {shown} / {limit} {unit}")
            if usage_error:
                cons.print(f"  [yellow]usage unavailable: {usage_error}[/yellow]")
