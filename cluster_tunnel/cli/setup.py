"""Setting clusters up: presets, add, remove."""

from __future__ import annotations

import json

import rich_click as click

from cluster_tunnel.cli import configcmd


class SetupCommandsMixin:
    """Mixin providing the `presets`, `add` and `remove` commands."""

    @click.command("presets")
    @click.pass_obj
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def presets_command(self, as_json: bool) -> None:
        """List the clusters ctun knows, ready for `ctun add <name> --preset <preset>`.

        Each preset brings the login host, what a login asks for, a description
        and the bundled budget guard. Where the cluster's operators publish the
        server's host-key fingerprint, the preset carries it (with the page it is
        published on), and `ctun -t <name> hostkey --pin --expect <fingerprint>`
        pins the key only if the server presents exactly that one.
        """
        from cluster_tunnel import api

        found = api.presets()
        if as_json:
            click.echo(json.dumps({"presets": [preset.as_dict() for preset in found]}, indent=2))
            return
        for preset in found:
            click.secho(f"{preset.name}", bold=True, nl=False)
            click.echo(f"  {preset.title}")
            click.echo(f"  host:        {preset.host or '(give --host)'}")
            login = "password + OTP" if preset.requires_otp else "password"
            if not preset.requires_password:
                login = "OTP" if preset.requires_otp else "key only"
            click.echo(f"  login:       {login}")
            if preset.user_hint:
                click.echo(f"  user:        {preset.user_hint}")
            if preset.budget is not None:
                click.echo(f"  budget:      {preset.budget.session_limit:g} {preset.budget.unit} per session")
            click.echo(f"  host key:    {preset.fingerprint or 'not published: compare and confirm it'}")
            click.echo()

    @click.command("add")
    @click.pass_obj
    @click.argument("name")
    @click.option("-p", "--preset", default=None, help="Start from a known cluster (see `ctun presets`).")
    @click.option("--host", default=None, help="Login host (required without a preset).")
    @click.option("-u", "--user", required=True, help="Your username on the cluster.")
    @click.option("--otp/--no-otp", "requires_otp", default=None, help="Whether a login asks for an OTP.")
    @click.option(
        "--no-password", "no_password", is_flag=True,
        help="A login asks for no service password (OTP or SSH key only).",
    )
    @click.option(
        "-l", "--session-limit", type=float, default=None,
        help="Budget per login session, in the preset's unit (default: the preset's).",
    )
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
    def add_command_(
        self,
        name: str,
        preset: str | None,
        host: str | None,
        user: str,
        requires_otp: bool | None,
        no_password: bool,
        session_limit: float | None,
        as_json: bool,
    ) -> None:
        """Add a cluster to the config: ctun add <name> --preset <preset> --user <you>.

        The entry goes into the config file in place: comments and layout stay,
        and the previous file is kept as `config.yaml.bak`. Without a config,
        one is created first, as `ctun config --init` would. A preset's budget
        script is copied next to the config unless one is there already.

        The host key is not pinned yet: run `ctun -t <name> hostkey --pin`
        next, with `--expect <fingerprint>` where the preset carries the one
        its operators publish. Then log in.
        """
        from cluster_tunnel import api

        try:
            added = api.add_cluster(
                name, user=user, preset=preset, host=host, requires_otp=requires_otp,
                requires_password=False if no_password else None, session_limit=session_limit,
                config_path=self.config_path,
            )
        except api.ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        if as_json:
            click.echo(json.dumps(added.as_dict(), indent=2))
            return
        made = "Created" if added.created else "Changed"
        via = f" from preset '{added.preset}'" if added.preset else ""
        click.echo(f"✓ Added '{name}'{via} ({user}@{added.entry['host']}).")
        click.echo(f"  {made} {added.config_path}" + (f"; the previous version is in {added.backup}." if added.backup else "."))
        if added.budget_script:
            click.echo(f"  Budget script: {added.budget_script}")
        click.echo()
        click.echo("Next:")
        if added.fingerprint:
            click.echo(f"  ctun -t {name} hostkey --pin --expect {added.fingerprint}")
            click.echo(f"      (published on {added.fingerprint_source})")
        else:
            click.echo(f"  ctun -t {name} hostkey --pin      # compare the key with your operators' before you confirm")
        click.echo(f"  ctun -t {name} login")

    @click.command("remove")
    @click.pass_obj
    @click.argument("name")
    @click.option("-y", "--yes", "assume_yes", is_flag=True, help="Remove without asking.")
    @click.option("-j", "--json", "as_json", is_flag=True, help="Machine-readable JSON output (needs --yes).")
    def remove_command(self, name: str, assume_yes: bool, as_json: bool) -> None:
        """Remove a cluster from the config: ctun remove <name> [--yes].

        Shows the change and asks first. A live tunnel to the cluster is closed
        and its session cleared; its pinned host keys go from its known_hosts
        file, unless another cluster uses the same host. The entry's lines go
        from the config file, which is kept as `config.yaml.bak`. Without a
        terminal, nothing is removed unless --yes.
        """
        from cluster_tunnel import api, configedit
        from cluster_tunnel import config as config_mod

        if as_json and not assume_yes:
            raise click.UsageError("--json goes with --yes: there is no one to ask.")
        config = self.load_config()
        if name not in config.clusters:
            known = ", ".join(sorted(config.clusters)) or "(none configured)"
            raise click.ClickException(f"Unknown cluster '{name}'. Configured: {known}")
        if not assume_yes:
            path = config_mod.resolve_config_path(self.config_path)
            try:
                edit = configedit.plan_remove_cluster(path, name)
            except configedit.EditError as exc:
                raise click.ClickException(f"Cannot remove '{name}' from {path}: {exc}.") from exc
            click.echo(f"Removing '{name}' from {path}:")
            click.echo()
            configcmd._echo_diff(edit.diff())
            click.echo()
            if not configcmd._stdin_is_tty():
                click.echo(f"Nothing removed. Run `ctun remove {name} --yes` to remove it.")
                return
            if not click.confirm(f"Remove '{name}'?", default=False):
                click.echo("Nothing removed.")
                return
        try:
            removed = api.remove_cluster(name, config_path=self.config_path)
        except (api.ConfigError, api.HostKeyError) as exc:
            raise click.ClickException(str(exc)) from exc
        if as_json:
            click.echo(json.dumps(removed.as_dict(), indent=2))
            return
        click.echo(f"✓ Removed '{name}'; the previous config is in {removed.backup}.")
        if removed.logged_out:
            click.echo("  Its tunnel was live and is closed now.")
        if removed.keys_removed:
            click.echo(f"  Removed its pinned host key from {removed.known_hosts_file}.")
        elif removed.keys_kept:
            click.echo(f"  Kept its pinned host key in {removed.known_hosts_file}: {removed.keys_kept}.")
