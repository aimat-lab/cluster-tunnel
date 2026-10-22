"""Your projects: every project in the job records (projects list), and renaming one (projects rename)."""

from __future__ import annotations

import rich_click as click

from cluster_tunnel.cli.errors import fail
from cluster_tunnel.cli.jobs import _JSON_OPTION


class ProjectsCommandsMixin:
    """Mixin providing the `projects` group: `projects list` and `projects rename`."""

    @click.group("projects")
    def projects_command() -> None:
        """Your projects: the `--project` of the job records, of all time."""

    @projects_command.command("list")
    @click.pass_obj
    @_JSON_OPTION
    def projects_list_command(self, as_json: bool) -> None:
        """List every project in the job records, with its number of jobs.

        A project exists only through the jobs whose record names it, so this
        reads every record on the cluster, however old. With a target
        (`-t <cluster>`), reports that cluster; without one, every configured
        cluster, each with a live tunnel (down ones are noted, never logged in
        to). Jobs without a project are not counted.
        """
        from cluster_tunnel import api

        config = self.load_config()
        if self.target:
            self.resolve_cluster(config)  # validate the target
        results = [
            entry.as_dict()
            for entry in api.projects(self.target or None, config=config, config_path=self.config_path)
        ]

        if as_json:
            import json

            click.echo(json.dumps({"clusters": results}, indent=2, ensure_ascii=False))
            return
        self._render_projects(results)

    @projects_command.command("rename")
    @click.pass_obj
    @click.argument("old")
    @click.argument("new")
    @_JSON_OPTION
    def projects_rename_command(self, old: str, new: str, as_json: bool) -> None:
        """Rename project OLD to NEW in every job record, with the projects nested in it.

        A `/` nests: renaming `htfm` to `archive/htfm` moves `htfm/mh` to
        `archive/htfm/mh` too. If NEW already exists, the jobs join it. Each
        change is logged in the job's record like `jobs edit`. With a target
        (`-t <cluster>`), renames on that cluster; without one, on every
        configured cluster with a live tunnel. A down cluster keeps OLD: run the
        command again once it is logged in; running it again is safe. The
        records go to the cluster in batches, a few dozen per SSH call.

        Exits 1 if the rename failed on a cluster, and with a target, 10
        (`login_required`) if its tunnel is down or 1 if it keeps no records.

        Example: ctun projects rename htfm archive/htfm
        """
        from cluster_tunnel import api

        config = self.load_config()
        if self.target:
            self.resolve_cluster(config)  # validate the target
        try:
            renames = api.rename_project(old, new, self.target or None, config=config, config_path=self.config_path)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from None
        if self.target and renames[0].job_records and not renames[0].live:
            fail(str(api.LoginRequired(self.target)), api.LoginRequired.exit_code)
        results = [entry.as_dict() for entry in renames]

        if as_json:
            import json

            click.echo(json.dumps({"clusters": results}, indent=2, ensure_ascii=False))
        else:
            for entry in results:
                if not self._render_cluster_problem(entry) or entry["renamed"]:
                    count = len(entry["renamed"])
                    self.cons.print(
                        f"[bold cyan]{entry['cluster']}[/bold cyan]  {old} → {new}  [dim]{count} jobs[/dim]"
                    )
        # A sweep only notes the clusters it cannot reach; a failure, or a target
        # that can't take the rename, is an error.
        if any(entry.error for entry in renames) or (self.target and not renames[0].job_records):
            raise SystemExit(1)

    def _render_cluster_problem(self, entry: dict) -> bool:
        """Prints why the cluster of `entry` was skipped; False when it was not."""
        name = entry["cluster"]
        if not entry["job_records"]:
            self.cons.print(f"[bold cyan]{name}[/bold cyan]  [dim]job_records is off[/dim]")
        elif not entry["live"]:
            self.cons.print(f"[bold cyan]{name}[/bold cyan]  [red]tunnel down[/red] [dim]— run `ctun -t {name} login`[/dim]")
        elif entry["error"]:
            self.cons.print(f"[bold cyan]{name}[/bold cyan]  [yellow]{entry['error']}[/yellow]")
        else:
            return False
        return True

    def _render_projects(self, results: list[dict]) -> None:
        cons = self.cons
        for entry in results:
            name = entry["cluster"]
            if self._render_cluster_problem(entry):
                continue
            if not entry["projects"]:
                cons.print(f"[bold cyan]{name}[/bold cyan]  [dim]no projects[/dim]")
            else:
                cons.print(f"[bold cyan]{name}[/bold cyan]")
                for project in entry["projects"]:
                    cons.print(f"  {project['project']}  [dim]{project['jobs']} jobs[/dim]")
