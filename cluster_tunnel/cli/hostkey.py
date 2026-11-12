"""Host-key pinning: hostkey."""

from __future__ import annotations

import rich_click as click

from cluster_tunnel.cli import configcmd
from cluster_tunnel.cli.errors import fail
from cluster_tunnel.constants import ExitCode


class HostKeyCommandsMixin:
    """Mixin providing the `hostkey` command."""

    @click.command("hostkey")
    @click.pass_obj
    @click.option(
        "--pin", "do_pin", is_flag=True,
        help="Pin the key the server presents now (shows the change, asks first).",
    )
    @click.option("-y", "--yes", "assume_yes", is_flag=True, help="With --pin: pin without asking.")
    @click.option(
        "--expect", "expect", default=None, metavar="SHA256:...",
        help="With --pin: pin without asking, only if the server presents this fingerprint "
        "(e.g. the one its operators publish; see `ctun presets`).",
    )
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def hostkey_command(self, do_pin: bool, assume_yes: bool, expect: str | None, as_json: bool) -> None:
        """Show the host key a cluster presents, or pin it: ctun -t <cluster> hostkey [--pin].

        The key is fetched over a fresh connection that stops before logging in
        (no password or OTP for the cluster; a ProxyJump host in your ssh config
        logs in as configured), and ssh itself says what a login would make of
        it: a key it trusts, a new one, or one that differs from the key it
        trusts. Until a key is pinned, logins accept an unknown key on first
        contact (StrictHostKeyChecking=accept-new).

        `--pin` stores the key in a known_hosts file next to the config and sets
        `strict_host_key_checking: yes` and `known_hosts_file` in the cluster's
        config entry (keeping config.yaml.bak), so later logins refuse any other
        key. Compare the fingerprint with the one your cluster's operators
        publish before you confirm. Without a terminal, `--pin` only shows the
        change, unless -y/--yes; a key that differs from the trusted one can only
        be re-pinned from a terminal.

        `--pin --expect <fingerprint>` pins without asking, but only if the
        server presents exactly that fingerprint (one its operators publish, as
        `ctun presets` lists them); any other key exits 14
        (`ctun-error: host_key_mismatch`) and nothing is pinned. So does a key
        that changes between showing it and pinning it.

        Exits 1 when the server presents a different key than the one logins trust.
        """
        from cluster_tunnel import api

        if assume_yes and not do_pin:
            raise click.UsageError("-y/--yes only goes with --pin.")
        if expect is not None and not do_pin:
            raise click.UsageError("--expect only goes with --pin.")
        if as_json and do_pin:
            raise click.UsageError("--json shows the key; use it without --pin.")

        config = self.load_config()
        name, cluster = self.resolve_cluster(config)
        try:
            status = api.host_key(name, config=config, config_path=self.config_path)
        except api.HostKeyError as exc:
            raise click.ClickException(str(exc)) from exc
        changed = status.trust == "changed"

        if as_json:
            import json

            click.echo(json.dumps(status.as_dict(), indent=2))
            if changed:
                raise SystemExit(1)
            return

        self._show_host_key(name, status)
        if not do_pin:
            if changed:
                raise click.ClickException(
                    f"'{name}' presents a different host key than the one logins trust. Don't log "
                    "in until you know why; if its operators announced a new key, re-pin it with "
                    f"`ctun -t {name} hostkey --pin` from a terminal."
                )
            return
        if expect is not None and status.offered.fingerprint != expect:
            fail(
                f"'{name}' presents {status.offered.fingerprint}, not the expected {expect}. Don't log "
                "in until you know why; nothing was pinned.",
                ExitCode.HOST_KEY_MISMATCH,
            )
        self._pin_host_key(name, cluster, status, assume_yes or expect is not None, changed, expect)

    def _show_host_key(self, name: str, status) -> None:
        key = status.offered
        click.echo(f"Host key of '{name}' ({key.host}):")
        click.echo(f"  {key.kind:<8} {key.fingerprint}")
        pin_it = f"`ctun -t {name} hostkey --pin`"
        if status.trust == "changed":
            click.secho("DIFFERENT from the key logins trust:", fg="red", bold=True)
            for pinned in status.pinned:
                click.secho(f"  {pinned.kind:<8} {pinned.fingerprint}", fg="red")
            for place in status.conflicts:
                click.secho(f"  (the trusted key is in {place})", fg="red")
        elif status.trust == "known" and status.matches:
            if status.strict:
                click.secho("pinned: logins accept only this key.", fg="green")
            else:
                click.secho(
                    f"matches the key in {status.known_hosts_file}, but logins still take a new key "
                    f"on first contact; {pin_it} makes them strict.",
                    fg="yellow",
                )
        elif status.trust == "known":
            click.echo(
                "known to ssh from an earlier login, not pinned by ctun"
                + ("" if status.strict else f"; pin it with {pin_it}")
                + "."
            )
        elif status.strict:
            where = status.known_hosts_file or "ssh's known_hosts"
            click.secho(
                f"not in {where}: logins are refused until the key is pinned with {pin_it}.",
                fg="yellow",
            )
        else:
            click.secho(
                f"not pinned: the next login accepts it on first contact. Pin it with {pin_it}.",
                fg="yellow",
            )

    def _pin_host_key(
        self, name: str, cluster, status, assume_yes: bool, changed: bool, expect: str | None = None
    ) -> None:
        """Show what pinning changes, then pin (after asking, unless --yes)."""
        from cluster_tunnel import api, configedit, hostkey
        from cluster_tunnel import config as config_mod

        if status.matches and status.strict:
            click.echo("Already pinned; nothing to do.")
            return

        path = config_mod.resolve_config_path(self.config_path)
        file = cluster.known_hosts_file or hostkey.default_file(path)
        try:
            edit = configedit.plan_host_key_pin(path, name, file)
        except configedit.EditError as exc:
            raise click.ClickException(f"Cannot pin the host key in {path}: {exc}.") from exc

        fingerprint = status.offered.fingerprint
        click.echo()
        click.echo(f"Pinning {fingerprint} for '{name}' in {file}.")
        if edit.changes:
            click.echo(f"Changing {path}:")
            click.echo()
            configcmd._echo_diff(edit.diff())
            click.echo()
            click.secho(
                "ctun 0.6.0 and older can't read a config with these keys: update ctun on every "
                "machine that uses this config.",
                fg="yellow",
            )
        click.echo()
        if expect is not None:
            click.secho("It is the fingerprint you expected.", fg="green")
        else:
            click.secho(
                "Compare the fingerprint with the one your cluster's operators publish before you confirm.",
                fg="yellow",
            )

        tty = configcmd._stdin_is_tty()
        if changed and not tty:
            raise click.ClickException(
                "The key differs from the one logins trust; re-pin it from a terminal (--yes does "
                "not apply to a changed key). Nothing was pinned."
            )
        if not assume_yes or changed:
            if not tty:
                click.echo(f"Nothing pinned. Run `ctun -t {name} hostkey --pin --yes` to pin it.")
                return
            question = "Replace the trusted key with this one?" if changed else "Pin this key?"
            if not click.confirm(question, default=False):
                click.echo("Nothing pinned.")
                return

        try:
            api.pin_host_key(name, fingerprint, replace=changed, config_path=self.config_path)
        except api.HostKeyMismatch as exc:  # the server changed its key since it was shown
            fail(str(exc), ExitCode.HOST_KEY_MISMATCH)
        except (api.HostKeyError, api.ConfigError) as exc:
            raise click.ClickException(str(exc)) from exc
        backup = f" The previous config is in {path.name}.bak." if edit.changes else ""
        click.echo(f"✓ Pinned: logins to '{name}' now accept only {fingerprint}.{backup}")
