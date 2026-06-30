"""File transfer over the live tunnel.

Transfers ride the existing SSH master via its control socket (see
``ssh.control_opts``), so they need no re-authentication. The actual transfer
tool sits behind a small :class:`TransferBackend` abstraction: today only rsync
is implemented, but a scp/sftp fallback can be added later by registering
another backend — the CLI never has to change.
"""

from __future__ import annotations

import shlex
import subprocess
from abc import ABC, abstractmethod
from typing import Literal, Sequence

from cluster_tunnel import ssh
from cluster_tunnel.ssh import ConnSpec

Direction = Literal["upload", "download"]


class TransferBackend(ABC):
    """A file-transfer engine that rides the cluster's existing SSH master."""

    name: str

    @abstractmethod
    def argv(
        self,
        spec: ConnSpec,
        *,
        direction: Direction,
        src: str,
        dest: str,
        dry_run: bool,
        extra: Sequence[str],
    ) -> list[str]:
        """Build the argv for one transfer. The remote side is on ``spec`` (the
        cluster); ``direction`` says whether ``src`` or ``dest`` is the remote one.
        """


class RsyncBackend(TransferBackend):
    """rsync over the multiplexed ssh master. Recursive (`-r`); paths verbatim."""

    name = "rsync"

    def argv(
        self,
        spec: ConnSpec,
        *,
        direction: Direction,
        src: str,
        dest: str,
        dry_run: bool,
        extra: Sequence[str],
    ) -> list[str]:
        # rsync's -e takes a command *string*; shlex.join quotes the socket path.
        ssh_cmd = shlex.join(["ssh", *ssh.control_opts(spec)])
        flags = ["-r"]
        if dry_run:
            flags.append("-n")
        flags += list(extra)
        # The remote path is bare on the CLI; prefix it with the ssh destination.
        # rsync argument order is SRC then DEST.
        if direction == "upload":
            local, remote = src, f"{spec.target}:{dest}"
        else:
            local, remote = f"{spec.target}:{src}", dest
        return ["rsync", *flags, "-e", ssh_cmd, local, remote]


_BACKENDS: dict[str, TransferBackend] = {
    backend.name: backend for backend in (RsyncBackend(),)
}


def get_backend(name: str = "rsync") -> TransferBackend:
    """Return the named transfer backend, or raise ``ValueError`` if unknown."""
    try:
        return _BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(_BACKENDS))
        raise ValueError(f"unknown transfer backend '{name}' (known: {known})") from None


def run_transfer(
    spec: ConnSpec,
    direction: Direction,
    src: str,
    dest: str,
    *,
    dry_run: bool = False,
    extra: Sequence[str] = (),
    backend: str = "rsync",
) -> int:
    """Run a transfer over the tunnel, streaming output; return the tool's exit code."""
    argv = get_backend(backend).argv(
        spec, direction=direction, src=src, dest=dest, dry_run=dry_run, extra=list(extra)
    )
    return subprocess.run(argv).returncode
