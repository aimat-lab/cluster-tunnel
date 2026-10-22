"""Tunnel lifecycle commands: login, status, logout."""

from __future__ import annotations

import rich_click as click


def _stdin_credentials():
    """Read ``{"password", "otp", "limit"}`` from stdin as login credentials.

    The secrets come through a pipe, never argv or the environment, like the
    login window's own hand-off. Read as UTF-8 whatever the platform's default
    (cp1252 on Windows would garble a non-ASCII password).
    """
    import json
    import math

    from cluster_tunnel import api

    try:
        raw = json.loads(click.get_binary_stream("stdin").read().decode("utf-8"))
    except UnicodeDecodeError:
        raise click.ClickException("--credentials-stdin: stdin is not UTF-8.") from None
    except ValueError as exc:
        raise click.ClickException(f"--credentials-stdin: stdin is not JSON ({exc}).") from None
    if not isinstance(raw, dict):
        raise click.ClickException("--credentials-stdin: expected a JSON object.")
    password, otp, limit = raw.get("password", ""), raw.get("otp"), raw.get("limit")
    if not isinstance(password, str) or not (otp is None or isinstance(otp, str)):
        raise click.ClickException("--credentials-stdin: password and otp must be strings.")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit)):
        raise click.ClickException("--credentials-stdin: limit must be a number (-1 for none) or null.")
    return api.Credentials(password=password, otp=otp or None, limit=None if limit is None else float(limit))


class TunnelCommandsMixin:
    """Mixin providing the tunnel lifecycle commands."""

    @click.command("login")
    @click.pass_obj
    @click.option("-i", "--interactive", is_flag=True, help="Pop up a window for a present human to enter the password and OTP.")
    @click.option("-l", "--limit", type=float, default=None, help="Session compute budget (overrides config session_limit; -1 = infinite/unguarded).")
    @click.option(
        "--credentials-stdin", is_flag=True,
        help='Read {"password", "otp", "limit"} as JSON on stdin instead of popping up a window (for front ends such as the VS Code extension).',
    )
    @click.option("--timeout", type=int, default=120, help="Seconds to wait for an interactive login to complete.")
    @click.option(
        "-v", "--verbose", count=True,
        help="Show ssh's own connection diagnostics on errors (repeatable: -v, -vv, -vvv).",
    )
    def login_command(
        self, interactive: bool, limit: float | None, credentials_stdin: bool, timeout: int, verbose: int
    ) -> None:
        """Authenticate once and open the persistent background tunnel.

        This is the only step that needs your password and one-time password
        (OTP). `login` opens a single long-lived SSH *master* connection
        (OpenSSH ControlMaster) and leaves its control socket running in the
        background. Every later `ctun -t <cluster> run -- ...` opens a
        lightweight channel inside that already-authenticated connection, so no
        password or OTP is needed again for the life of the tunnel.

        The tunnel stays up while it is used and for `control_persist` after
        its last command (configurable; 7d by default). It closes earlier if
        the network is gone for longer than the keepalives allow (about 10
        minutes by default) or when you `logout`. Logging in also starts a
        fresh budget *session*: the compute-budget guard measures usage from
        this moment onward, and the limit comes from `-l/--limit`, falling back
        to the cluster's configured `session_limit`.

        Use `-i/--interactive` when ctun is driven by a tool with no terminal of
        its own (e.g. a coding agent): a small pop-up lets a present human type
        the service password and one-time passcode (OTP) in separate fields,
        which are fed to ssh at their respective prompts — the agent never sees
        the secrets. A front end that asks the human itself passes them with
        `--credentials-stdin` instead. An agent started by such a front end (the
        ctun VS Code extension) finds it in `CTUN_LOGIN_BRIDGE`, and `-i` lets the
        front end ask in its own input boxes instead of the pop-up.

        Pass `-v` (or `-vv`/`-vvv`) to surface ssh's own connection diagnostics
        when a login fails — useful for debugging host, auth, or network errors.

        On Windows the tunnel runs inside WSL 2 (experimental): `login` first
        checks that WSL has ssh, rsync and python3, and names the distro it uses.
        """
        from cluster_tunnel import api

        if interactive and credentials_stdin:
            raise click.UsageError("Give -i/--interactive or --credentials-stdin, not both.")

        config = self.load_config()
        name, _ = self.resolve_cluster(config)
        credentials = _stdin_credentials() if credentials_stdin else None
        try:
            result = api.login(
                name, interactive=interactive, credentials=credentials, limit=limit, timeout=timeout,
                verbose=verbose, config=config, config_path=self.config_path,
            )
        except api.LoginError as exc:
            if exc.diagnostics:
                click.echo(exc.diagnostics, err=True)
            hint = " Re-run with -v (or -vv/-vvv) for ssh diagnostics." if exc.ssh_failed and not verbose else ""
            raise click.ClickException(f"{exc}{hint}") from exc

        # The styled top line of the success panel.
        if result.bridged:
            headline = f"[green]✓[/green] Tunnel to [cyan]{name}[/cyan] [green]established[/green] [dim](through the login bridge)[/dim]."
        elif result.already_live:
            headline = f"[yellow]●[/yellow] Tunnel to [cyan]{name}[/cyan] is [yellow]already live[/yellow]."
        elif result.interactive:
            headline = f"[green]✓[/green] Tunnel to [cyan]{name}[/cyan] [green]established[/green] [dim](interactive)[/dim]."
        else:
            headline = f"[green]✓[/green] Tunnel to [cyan]{name}[/cyan] [green]established[/green]."

        sess = result.session
        if sess.get("limit") is not None:
            budget_line = f"  budget   [orange3]{sess['limit']} {sess['unit']}[/orange3]"
        else:
            budget_line = "  budget   [dim]unguarded (no limit set)[/dim]"

        from rich.console import Group
        from rich.panel import Panel

        body = [headline, "", f"  target   [dim]{result.target}[/dim]"]
        if result.via:
            body.append(f"  via      [dim]WSL distro {result.via}[/dim]")
        body.append(budget_line)
        if result.session_kept:
            body.append("  [dim]note: existing session kept; re-auth would reset the budget window[/dim]")
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

        from cluster_tunnel import api

        config = self.load_config()
        if self.target:
            self.resolve_cluster(config)

        # Live budget usage requires a remote probe over the tunnel, so it only
        # runs for an explicitly targeted cluster — never as a side effect of
        # the all-clusters overview.
        rows = [
            row.as_dict()
            for row in api.status(
                self.target or None, usage=bool(self.target), config=config, config_path=self.config_path
            )
        ]

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
        """Close the tunnel and clear the session.

        With a target (`-t <cluster>`), logs out that cluster. Without one, logs
        out every configured cluster.
        """
        from cluster_tunnel import api

        config = self.load_config()
        if self.target:
            self.resolve_cluster(config)  # validate the target

        closed = api.logout(self.target or None, config=config, config_path=self.config_path)
        if not closed:
            click.echo("No clusters configured. Run `ctun config --init`.")
            return

        for item in closed:
            if item.was_live:
                click.echo(f"Tunnel to '{item.cluster}' closed; session cleared.")
            else:
                click.echo(f"No live tunnel for '{item.cluster}'; session cleared.")
