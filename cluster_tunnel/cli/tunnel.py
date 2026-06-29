"""Tunnel lifecycle commands: login, status, logout."""

from __future__ import annotations

import rich_click as click


class TunnelCommandsMixin:
    """Mixin providing the tunnel lifecycle commands."""

    @click.command("login")
    @click.pass_obj
    @click.option("-i", "--interactive", is_flag=True, help="Pop up a window for a present human to enter the password and OTP.")
    @click.option("-l", "--limit", type=float, default=None, help="Session compute budget (overrides config session_limit; -1 = infinite/unguarded).")
    @click.option("--timeout", type=int, default=120, help="Seconds to wait for an interactive login to complete.")
    @click.option(
        "-v", "--verbose", count=True,
        help="Show ssh's own connection diagnostics on errors (repeatable: -v, -vv, -vvv).",
    )
    def login_command(self, interactive: bool, limit: float | None, timeout: int, verbose: int) -> None:
        """Authenticate once and open the persistent background tunnel.

        This is the only step that needs your password and one-time password
        (OTP). `login` opens a single long-lived SSH *master* connection
        (OpenSSH ControlMaster) and leaves its control socket running in the
        background. Every later `ctun -t <cluster> run -- ...` opens a
        lightweight channel inside that already-authenticated connection, so no
        password or OTP is needed again for the life of the tunnel.

        The tunnel stays up for `control_persist` (configurable; 12h by default)
        or until the network drops or you `logout`. Logging in also starts a
        fresh budget *session*: the compute-budget guard measures usage from
        this moment onward, and the limit comes from `-l/--limit`, falling back
        to the cluster's configured `session_limit`.

        Use `-i/--interactive` when ctun is driven by a tool with no terminal of
        its own (e.g. a coding agent): a small pop-up lets a present human type
        the service password and one-time passcode (OTP) in separate fields,
        which are fed to ssh at their respective prompts — the agent never sees
        the secrets.

        Pass `-v` (or `-vv`/`-vvv`) to surface ssh's own connection diagnostics
        when a login fails — useful for debugging host, auth, or network errors.
        """
        from cluster_tunnel import cmdlog, session, ssh

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
        headline = ""  # the styled top line of the success panel

        if ssh.is_live(spec):
            headline = f"[yellow]●[/yellow] Tunnel to [cyan]{name}[/cyan] is [yellow]already live[/yellow]."
        elif interactive:
            from cluster_tunnel import popup

            if not popup.gui_available():
                raise click.ClickException(
                    f"No display for the login dialog; run `ctun -t {name} login` from a terminal."
                )
            creds = popup.prompt_credentials(name, spec.target, default_limit, unit)
            if creds is None:
                raise click.ClickException("Login cancelled.")
            if not popup.login_with_password(spec, creds.password, creds.otp, timeout, verbose):
                hint = "" if verbose else " Re-run with -v (or -vv/-vvv) for ssh diagnostics."
                raise click.ClickException(
                    f"Interactive login to '{name}' failed or timed out.{hint}"
                )
            established = True
            chosen_limit = creds.limit
            headline = f"[green]✓[/green] Tunnel to [cyan]{name}[/cyan] [green]established[/green] [dim](interactive)[/dim]."
        else:
            rc = ssh.open_master(spec, verbose)
            if rc != 0 or not ssh.is_live(spec):
                if verbose:
                    detail = (ssh.check(spec).stderr or "").strip()
                    if detail:
                        click.echo(detail, err=True)
                hint = "" if verbose else " Re-run with -v (or -vv/-vvv) for ssh diagnostics."
                raise click.ClickException(
                    f"Failed to open tunnel to '{name}' (ssh exit {rc}).{hint}"
                )
            established = True
            headline = f"[green]✓[/green] Tunnel to [cyan]{name}[/cyan] [green]established[/green]."

        # A session limit of -1 (or any negative) means "infinite" — i.e. no
        # budget guard at all, same as leaving the limit unset.
        if chosen_limit is not None and chosen_limit < 0:
            chosen_limit = None

        # A session begins on a fresh authentication; re-auth resets the window.
        note = None
        if established or session.load(name) is None:
            cmdlog.clear(name)  # fresh session: start the command log empty
            sess = session.start(name, limit=chosen_limit, unit=unit)
        else:
            sess = session.load(name)
            note = "existing session kept; re-auth would reset the budget window"

        if sess.get("limit") is not None:
            budget_line = f"  budget   [orange3]{sess['limit']} {sess['unit']}[/orange3]"
        else:
            budget_line = "  budget   [dim]unguarded (no limit set)[/dim]"

        from rich.console import Group
        from rich.panel import Panel

        body = [headline, "", f"  target   [dim]{spec.target}[/dim]", budget_line]
        if note:
            body.append(f"  [dim]note: {note}[/dim]")
        self.cons.print(
            Panel(
                Group(*body),
                title="[bold]login[/bold]",
                title_align="left",
                border_style="bright_black",
                padding=(0, 1),
                expand=False,
            )
        )

    @click.command("status")
    @click.pass_obj
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def status_command(self, as_json: bool) -> None:
        """Show tunnel + session status (all clusters, or one with -t)."""
        from datetime import datetime

        from rich.table import Table

        from cluster_tunnel import budget as budget_mod
        from cluster_tunnel import cmdlog
        from cluster_tunnel import config as config_mod
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
            live = ssh.is_live(spec)
            sess = session.load(name)

            # Live budget usage requires a remote probe over the tunnel, so we
            # only run it for an explicitly targeted, guarded, live cluster —
            # never as a side effect of the all-clusters overview.
            used: float | None = None
            used_error: str | None = None
            if self.target and live and sess and sess.get("limit") is not None:
                cluster = config.clusters[name]
                if cluster.budget:
                    try:
                        config_path = config_mod.resolve_config_path(self.config_path)
                        script = config_mod.budget_script_path(
                            config_path, name, cluster.budget.script
                        )
                        user = budget_mod.remote_user(spec, cluster)
                        used = budget_mod.used_since(
                            spec, script, int(sess["start_epoch"]), user
                        )
                    except Exception as exc:  # noqa: BLE001
                        used_error = str(exc)

            rows.append(
                {
                    "cluster": name,
                    "live": live,
                    "session": sess,
                    "commands": cmdlog.summary(name),
                    "used": used,
                    "used_error": used_error,
                }
            )

        if as_json:
            import json

            click.echo(json.dumps(rows, indent=2))
            return

        table = Table(
            show_header=True,
            header_style="bold",
            border_style="bright_black",
            expand=True,
        )
        table.add_column("Cluster", style="cyan", no_wrap=True)
        table.add_column("Tunnel")
        table.add_column("Session started")
        table.add_column("Budget")
        table.add_column("Used")
        table.add_column("Commands", justify="left")
        table.add_column("Last command at")
        for r in rows:
            live = "[green]live[/green]" if r["live"] else "[red]down[/red]"
            sess = r["session"]
            if sess:
                started = datetime.fromtimestamp(sess["start_epoch"]).strftime("%Y-%m-%d %H:%M")
                lim = sess.get("limit")
                if lim is None:
                    budget = "unguarded"
                elif lim < 0:
                    budget = "unlimited"
                else:
                    budget = f"limit {lim} {sess['unit']}"
            else:
                started = budget = "—"

            if r["used_error"]:
                used_cell = "[yellow]unavailable[/yellow]"
            elif r["used"] is not None:
                used_cell = f"[orange3]{r['used']}[/orange3] {sess['unit']}"
            else:
                used_cell = "—"

            cmds = r["commands"]
            count = str(cmds["count"])
            last = (
                datetime.fromtimestamp(cmds["last_epoch"]).strftime("%Y-%m-%d %H:%M")
                if cmds["last_epoch"]
                else "—"
            )
            table.add_row(r["cluster"], live, started, budget, used_cell, count, last)

        if not rows:
            click.echo("No clusters configured. Run `ctun config --init`.")
        else:
            self.cons.print(table)

    @click.command("logout")
    @click.pass_obj
    def logout_command(self) -> None:
        """Close the tunnel and clear the session."""
        from cluster_tunnel import cmdlog, session, ssh

        config = self.load_config()
        name, _ = self.resolve_cluster(config)
        spec = ssh.conn_spec(config, name)
        was_live = ssh.is_live(spec)
        ssh.close(spec)
        session.clear(name)
        cmdlog.clear(name)
        if was_live:
            click.echo(f"Tunnel to '{name}' closed; session cleared.")
        else:
            click.echo(f"No live tunnel for '{name}'; session cleared.")
