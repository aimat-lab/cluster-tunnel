"""File-transfer commands: upload and download (rsync over the live tunnel)."""

from __future__ import annotations

import rich_click as click

from cluster_tunnel.cli.errors import fail
from cluster_tunnel.constants import ExitCode

_CTX = {"ignore_unknown_options": True}


class TransferCommandsMixin:
    """Mixin providing the `upload` and `download` commands."""

    def _transfer(self, direction: str, src: str, dest: str, dry_run: bool, extra: tuple) -> None:
        """Shared logic: resolve the cluster, ensure the tunnel is live, transfer."""
        from cluster_tunnel import ssh
        from cluster_tunnel import transfer as transfer_mod

        config = self.load_config()
        name, _ = self.resolve_cluster(config)
        spec = ssh.conn_spec(config, name)

        if not ssh.is_live(spec):
            fail(
                f"No live tunnel for '{name}'. Run `ctun -t {name} login` "
                f"(or `ctun -t {name} login --interactive`) first.",
                ExitCode.LOGIN_REQUIRED,
            )

        # Transfers are not budget-guarded — moving data isn't compute.
        raise SystemExit(
            transfer_mod.run_transfer(
                spec, direction, src, dest, dry_run=dry_run, extra=extra
            )
        )

    @click.command("upload", context_settings=_CTX)
    @click.pass_obj
    @click.option("-n", "--dry-run", is_flag=True, help="Show what rsync would transfer; don't copy.")
    @click.argument("src")
    @click.argument("dest")
    @click.argument("rsync_args", nargs=-1, type=click.UNPROCESSED)
    def upload_command(self, dry_run: bool, src: str, dest: str, rsync_args: tuple) -> None:
        """Upload a local path to the cluster: ctun -t <cluster> upload <local-src> <remote-dest>.

        Copies recursively (rsync -r) over the live tunnel — no re-auth. The
        remote DEST is written bare (relative paths resolve to your remote
        $HOME). Trailing slashes follow rsync semantics (`dir/` copies the
        contents, `dir` copies the directory). Extra rsync flags may follow `--`,
        e.g. `upload ./data data -- --exclude='*.tmp' -z`.
        """
        self._transfer("upload", src, dest, dry_run, rsync_args)

    @click.command("download", context_settings=_CTX)
    @click.pass_obj
    @click.option("-n", "--dry-run", is_flag=True, help="Show what rsync would transfer; don't copy.")
    @click.argument("src")
    @click.argument("dest")
    @click.argument("rsync_args", nargs=-1, type=click.UNPROCESSED)
    def download_command(self, dry_run: bool, src: str, dest: str, rsync_args: tuple) -> None:
        """Download a cluster path to the local machine: ctun -t <cluster> download <remote-src> <local-dest>.

        Copies recursively (rsync -r) over the live tunnel — no re-auth. The
        remote SRC is written bare (relative paths resolve to your remote $HOME).
        Trailing slashes follow rsync semantics. Extra rsync flags may follow
        `--`, e.g. `download results out -- --info=progress2`.
        """
        self._transfer("download", src, dest, dry_run, rsync_args)
