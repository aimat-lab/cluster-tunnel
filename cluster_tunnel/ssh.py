"""OpenSSH ControlMaster wrapper — the persistent tunnel transport.

`ctun` is stateless and short-lived; persistence lives in a backgrounded ssh
master process and its control socket. These helpers build and drive that master.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cluster_tunnel import config as config_mod
from cluster_tunnel import paths
from cluster_tunnel.config import Config


@dataclass
class ConnSpec:
    """Everything needed to talk to one cluster's tunnel."""

    name: str
    target: str  # ssh destination: alias or user@host
    socket: Path
    control_persist: str
    server_alive_interval: int
    server_alive_count_max: int
    identity_file: str | None


def conn_spec(config: Config, name: str) -> ConnSpec:
    """Build the connection spec for a cluster, merging defaults."""
    cluster = config_mod.get_cluster(config, name)
    d = config.defaults
    socket_dir = Path(d.socket_dir or str(paths.socket_dir())).expanduser()
    return ConnSpec(
        name=name,
        target=config_mod.resolve_target(cluster),
        socket=socket_dir / name,
        control_persist=cluster.control_persist or d.control_persist,
        server_alive_interval=cluster.server_alive_interval or d.server_alive_interval,
        server_alive_count_max=cluster.server_alive_count_max or d.server_alive_count_max,
        identity_file=cluster.identity_file,
    )


def _socket_opts(spec: ConnSpec) -> list[str]:
    """Options to attach to an existing master via its control socket."""
    return ["-S", str(spec.socket)]


def open_master_argv(spec: ConnSpec) -> list[str]:
    """argv for establishing the persistent master connection."""
    argv = [
        "ssh",
        "-M",
        "-S", str(spec.socket),
        "-o", f"ControlPersist={spec.control_persist}",
        "-o", f"ServerAliveInterval={spec.server_alive_interval}",
        "-o", f"ServerAliveCountMax={spec.server_alive_count_max}",
        "-o", "StrictHostKeyChecking=accept-new",
    ]
    if spec.identity_file:
        argv += ["-i", str(Path(spec.identity_file).expanduser())]
    argv += ["-N", "-f", spec.target]
    return argv


def ensure_clean_socket(spec: ConnSpec) -> None:
    """Remove a stale control socket so a fresh master can be created.

    A dead socket file (left by a master that exited uncleanly) makes ssh refuse
    to multiplex ("ControlSocket ... already exists"); drop it if it isn't live.
    """
    if spec.socket.exists() and not is_live(spec):
        spec.socket.unlink(missing_ok=True)


def open_master(spec: ConnSpec) -> int:
    """Run the interactive master ssh in the current TTY; return its exit code.

    `-f` backgrounds the master after authentication, so this returns promptly
    once the OTP has been entered (or immediately for key-based auth).
    """
    spec.socket.parent.mkdir(parents=True, exist_ok=True)
    ensure_clean_socket(spec)
    return subprocess.run(open_master_argv(spec)).returncode


def is_live(spec: ConnSpec) -> bool:
    """True if the master connection is alive (`ssh -O check`)."""
    res = subprocess.run(
        ["ssh", *_socket_opts(spec), "-O", "check", spec.target],
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


def run(spec: ConnSpec, tokens: list[str], *, tty: bool = False) -> int:
    """Run a command over the live tunnel, streaming I/O; return remote exit code."""
    argv = ["ssh", *_socket_opts(spec), "-o", "BatchMode=yes"]
    if tty:
        argv.append("-tt")
    argv += [spec.target, shlex.join(tokens)]
    return subprocess.run(argv).returncode


def capture(spec: ConnSpec, tokens: list[str]) -> subprocess.CompletedProcess:
    """Run a command over the tunnel and capture its output (no streaming)."""
    return subprocess.run(
        ["ssh", *_socket_opts(spec), "-o", "BatchMode=yes", spec.target, shlex.join(tokens)],
        capture_output=True,
        text=True,
    )


def close(spec: ConnSpec) -> bool:
    """Cleanly tear down the master (`ssh -O exit`)."""
    res = subprocess.run(
        ["ssh", *_socket_opts(spec), "-O", "exit", spec.target],
        capture_output=True,
        text=True,
    )
    return res.returncode == 0


def probe(
    spec: ConnSpec, script_path: Path, start_epoch: int, user: str
) -> subprocess.CompletedProcess:
    """Run a budget script on the login node, fed over the tunnel via `bash -s`.

    Passes positional args ``<start_epoch> <cluster> <user>`` to the script and
    captures its stdout/stderr. The caller interprets the result.
    """
    remote = "bash -s -- {} {} {}".format(
        shlex.quote(str(start_epoch)), shlex.quote(spec.name), shlex.quote(user)
    )
    with open(script_path, "rb") as fh:
        return subprocess.run(
            ["ssh", *_socket_opts(spec), "-o", "BatchMode=yes", spec.target, remote],
            stdin=fh,
            capture_output=True,
            text=True,
        )
