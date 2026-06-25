"""Configuration command: config."""

from __future__ import annotations

import rich_click as click


class ConfigCommandsMixin:
    """Mixin providing the `config` command."""

    @click.command("config")
    @click.pass_obj
    @click.option("--path", "show_path", is_flag=True, help="Print the config file path and exit.")
    @click.option("--show", "show_contents", is_flag=True, help="Print the resolved config and exit.")
    @click.option("--validate", "do_validate", is_flag=True, help="Validate the config and exit.")
    @click.option("--init", "do_init", is_flag=True, help="Create a starter config if none exists.")
    def config_command(
        self, show_path: bool, show_contents: bool, do_validate: bool, do_init: bool
    ) -> None:
        """Open the config file in $EDITOR (or inspect it)."""
        import yaml

        from cluster_tunnel import config as config_mod

        path = config_mod.resolve_config_path(self.config_path)

        if do_init:
            created = config_mod.setup_if_necessary(self.config_path)
            click.echo(f"Config ready at {created}")
            return

        if show_path:
            click.echo(str(path))
            return

        if not path.exists():
            raise click.UsageError(f"No config at {path}. Run `ctun config --init` first.")

        if do_validate:
            try:
                config_mod.load_config(self.config_path)
            except Exception as exc:  # noqa: BLE001
                raise click.ClickException(f"Invalid config: {exc}") from exc
            click.echo(f"OK — {path} is valid.")
            return

        if show_contents:
            config = config_mod.load_config(self.config_path)
            click.echo(yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False).rstrip())
            return

        click.edit(filename=str(path))
