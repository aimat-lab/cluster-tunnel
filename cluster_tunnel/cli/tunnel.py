"""Tunnel lifecycle commands: login, status, logout."""

from __future__ import annotations

import rich_click as click


class TunnelCommandsMixin:
    """Mixin providing the tunnel lifecycle commands."""

    @click.command("login")
    @click.pass_obj
    @click.option("--interactive", is_flag=True, help="Pop up a window for a present human to enter the OTP.")
    @click.option("--limit", type=float, default=None, help="Session compute budget (overrides config session_limit).")
    @click.option("--timeout", type=int, default=120, help="Seconds to wait for an interactive login to complete.")
    def login_command(self, interactive: bool, limit: float | None, timeout: int) -> None:
        """Authenticate (enter OTP) and open the persistent tunnel."""
        from cluster_tunnel import session, ssh

        config = self.load_config()
        name, cluster = self.resolve_cluster(config)
        spec = ssh.conn_spec(config, name)

        # Default limit: --limit, else the cluster's configured session_limit.
        default_limit = limit
        if default_limit is None and cluster.budget is not None:
            default_limit = cluster.budget.session_limit
        unit = cluster.budget.unit if cluster.budget else "units"

        established = False
        chosen_limit = default_limit

        if ssh.is_live(spec):
            click.echo(f"Tunnel to '{name}' is already live.")
        elif interactive:
            from cluster_tunnel import popup

            if not popup.gui_available():
                raise click.ClickException(
                    f"No display for the login dialog; run `ctun -t {name} login` from a terminal."
                )
            creds = popup.prompt_credentials(name, spec.target, default_limit)
            if creds is None:
                raise click.ClickException("Login cancelled.")
            if not popup.login_with_password(spec, creds.password, timeout):
                raise click.ClickException(
                    f"Interactive login to '{name}' failed or timed out."
                )
            established = True
            chosen_limit = creds.limit
            click.echo(f"Tunnel to '{name}' established (interactive).")
        else:
            if ssh.open_master(spec) != 0 or not ssh.is_live(spec):
                raise click.ClickException(f"Failed to open tunnel to '{name}'.")
            established = True
            click.echo(f"Tunnel to '{name}' established.")

        # A session begins on a fresh authentication; re-auth resets the window.
        if established or session.load(name) is None:
            sess = session.start(name, limit=chosen_limit, unit=unit)
        else:
            sess = session.load(name)
            click.echo("(existing session kept; re-auth would reset the budget window)")

        if sess.get("limit") is not None:
            click.echo(f"Session budget: limit = {sess['limit']} {sess['unit']}.")
        else:
            click.echo("Session budget: unguarded (no limit set).")

    @click.command("status")
    @click.pass_obj
    @click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def status_command(self, as_json: bool) -> None:
        """Show tunnel + session status (all clusters, or one with -t)."""
        from datetime import datetime

        from rich.table import Table

        from cluster_tunnel import session, ssh

        config = self.load_config()
        if self.target:
            self.resolve_cluster(config)
            names = [self.target]
        else:
            names = sorted(config.clusters)

        rows = []
        for name in names:
            spec = ssh.conn_spec(config, name)
            rows.append({"cluster": name, "live": ssh.is_live(spec), "session": session.load(name)})

        if as_json:
            import json

            click.echo(json.dumps(rows, indent=2))
            return

        table = Table(show_header=True, header_style="bold", border_style="bright_black")
        table.add_column("Cluster", style="cyan", no_wrap=True)
        table.add_column("Tunnel")
        table.add_column("Session started")
        table.add_column("Budget")
        for r in rows:
            live = "[green]live[/green]" if r["live"] else "[red]down[/red]"
            sess = r["session"]
            if sess:
                started = datetime.fromtimestamp(sess["start_epoch"]).strftime("%Y-%m-%d %H:%M")
                budget = (
                    f"limit {sess['limit']} {sess['unit']}"
                    if sess.get("limit") is not None
                    else "unguarded"
                )
            else:
                started = budget = "—"
            table.add_row(r["cluster"], live, started, budget)

        if not rows:
            click.echo("No clusters configured. Run `ctun config --init`.")
        else:
            self.cons.print(table)

    @click.command("logout")
    @click.pass_obj
    def logout_command(self) -> None:
        """Close the tunnel and clear the session."""
        from cluster_tunnel import session, ssh

        config = self.load_config()
        name, _ = self.resolve_cluster(config)
        spec = ssh.conn_spec(config, name)
        was_live = ssh.is_live(spec)
        ssh.close(spec)
        session.clear(name)
        if was_live:
            click.echo(f"Tunnel to '{name}' closed; session cleared.")
        else:
            click.echo(f"No live tunnel for '{name}'; session cleared.")
