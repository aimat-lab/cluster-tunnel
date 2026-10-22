"""OpenSSH ControlMaster wrapper — the persistent tunnel transport.

`ctun` is stateless and short-lived; persistence lives in a backgrounded ssh
master process and its control socket. These helpers build and drive that master.

On Windows every ssh runs inside WSL (see :mod:`cluster_tunnel.wsl`): the argv
builders below are the same on every platform, and :func:`wsl.launch` adds the
``wsl.exe`` prefix wherever a process is started.
"""

from __future__ import annotations

import codecs
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath
from typing import IO, Callable, Optional

from cluster_tunnel import config as config_mod
from cluster_tunnel import constants, paths, wsl
from cluster_tunnel.config import Config


@dataclass
class ConnSpec:
    """Everything needed to talk to one cluster's tunnel."""

    name: str
    target: str  # ssh destination: alias or user@host
    socket: PurePath  # on Windows, a POSIX path inside WSL
    control_persist: str
    server_alive_interval: int
    server_alive_count_max: int
    identity_file: str | None
    ssh_config: str | None = None  # alternative ssh config file, passed as -F
    wsl_distro: str | None = None  # Windows only; None means WSL's default distro
    strict_host_key_checking: str = "accept-new"  # "yes" once the host key is pinned
    known_hosts_file: str | None = None  # the pinned key's file; None means ssh's own


def conn_spec(config: Config, name: str) -> ConnSpec:
    """Build the connection spec for a cluster, merging defaults."""
    cluster = config_mod.get_cluster(config, name)
    d = config.defaults
    if wsl.enabled():
        # The master runs inside WSL, so its socket lives there too; ssh expands `~`.
        socket: PurePath = PurePosixPath(d.socket_dir or wsl.DEFAULT_SOCKET_DIR) / name
    else:
        socket = Path(d.socket_dir or str(paths.socket_dir())).expanduser() / name
    return ConnSpec(
        name=name,
        target=config_mod.resolve_target(cluster),
        socket=socket,
        control_persist=cluster.control_persist or d.control_persist,
        server_alive_interval=cluster.server_alive_interval or d.server_alive_interval,
        server_alive_count_max=cluster.server_alive_count_max or d.server_alive_count_max,
        identity_file=cluster.identity_file,
        ssh_config=cluster.ssh_config or d.ssh_config,
        wsl_distro=d.wsl_distro,
        strict_host_key_checking=cluster.strict_host_key_checking,
        known_hosts_file=cluster.known_hosts_file,
    )


def _ssh_path(path: str) -> str:
    """A path from the config, as the ssh that reads it should see it.

    On Windows ssh runs inside WSL and the path is a WSL path, so it passes
    through untouched (ssh expands ``~`` in ``-i`` itself). Elsewhere ``~`` is
    expanded here.
    """
    return path if wsl.enabled() else str(Path(path).expanduser())


def _config_opts(spec: ConnSpec) -> list[str]:
    """``-F`` for an alternative ssh config file, when one is configured."""
    return ["-F", _ssh_path(spec.ssh_config)] if spec.ssh_config else []


def _identity_opts(spec: ConnSpec) -> list[str]:
    """``-i`` for an explicit identity (key) file, when one is configured."""
    return ["-i", _ssh_path(spec.identity_file)] if spec.identity_file else []


def _known_hosts_opts(spec: ConnSpec) -> list[str]:
    """Where ssh looks up the server's key: a pinned cluster's ``known_hosts_file`` only.

    That file then replaces ssh's own known_hosts (quoted: ssh splits the value
    at spaces), and the global file and DNS records are not consulted, so the
    pinned key is the only one trusted. Unpinned, ssh's own files apply.
    """
    if not spec.known_hosts_file:
        return []
    return [
        "-o", f'UserKnownHostsFile="{_ssh_path(spec.known_hosts_file)}"',
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "VerifyHostKeyDNS=no",
    ]


def _host_key_opts(spec: ConnSpec) -> list[str]:
    """How the master checks the server's host key.

    ``accept-new`` records an unknown key on first contact and refuses a changed
    one; ``yes`` accepts only a key already known (see :func:`_known_hosts_opts`).
    Command-line options win over the ssh config, so these always apply.
    """
    return ["-o", f"StrictHostKeyChecking={spec.strict_host_key_checking}", *_known_hosts_opts(spec)]


def _pinned_opts(spec: ConnSpec) -> list[str]:
    """For a pinned or strict cluster, its host-key options on every other ssh too.

    Commands ride the master, but should its socket vanish, ssh connects on its
    own (batch mode: only with a key); this keeps the pin on that connection.
    An unpinned cluster's commands keep ssh's own settings, as before.
    """
    pinned = spec.known_hosts_file or spec.strict_host_key_checking == "yes"
    return _host_key_opts(spec) if pinned else []


def _socket_opts(spec: ConnSpec) -> list[str]:
    """Options to attach to an existing master via its control socket."""
    return ["-S", str(spec.socket), *_config_opts(spec), *_pinned_opts(spec)]


def control_opts(spec: ConnSpec) -> list[str]:
    """ssh options that ride the existing master as a non-master, in batch mode.

    Transfer tools (scp/sftp/rsync) reference the control socket via
    ``-o ControlPath=`` rather than ssh's ``-S``. ``ControlMaster=no`` makes the
    tool reuse the master but never *create* one, and ``BatchMode=yes`` means a
    dead tunnel fails immediately instead of prompting — matching ``run``'s
    fail-closed contract. This is the single source of truth for "attach to the
    master" shared by every transfer backend.
    """
    return [
        "-o", f"ControlPath={spec.socket}",
        "-o", "ControlMaster=no",
        "-o", "BatchMode=yes",
        *_config_opts(spec),
        *_identity_opts(spec),
        *_pinned_opts(spec),
    ]


def open_master_argv(spec: ConnSpec, verbose: int = 0) -> list[str]:
    """argv for establishing the persistent master connection.

    ``verbose`` (0-3) adds ssh's own ``-v``/``-vv``/``-vvv`` flags so connection
    failures (auth, host key, network) print ssh's diagnostics.
    """
    argv = ["ssh", "-M", "-S", str(spec.socket)]
    if verbose:
        argv.append("-" + "v" * min(verbose, 3))
    argv += [
        "-o", f"ControlPersist={spec.control_persist}",
        "-o", f"ServerAliveInterval={spec.server_alive_interval}",
        "-o", f"ServerAliveCountMax={spec.server_alive_count_max}",
        *_host_key_opts(spec),
        *_config_opts(spec),
        *_identity_opts(spec),
    ]
    argv += ["-N", "-f", spec.target]
    return argv


#: A connection that only meets the server: a fresh one (no master, no
#: multiplexing), no prompts, every authentication method off, so it fails
#: right after the host-key exchange without a password or OTP, and nothing
#: forwarded, so a server that does let it in gets no agent, X11 or ports.
#: Should a server let it in unauthenticated, ``true`` is all that runs.
_NO_LOGIN = [
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=20",
    "-o", "PubkeyAuthentication=no",
    "-o", "PasswordAuthentication=no",
    "-o", "KbdInteractiveAuthentication=no",
    "-o", "HostbasedAuthentication=no",
    "-o", "GSSAPIAuthentication=no",
    "-o", "ForwardAgent=no",
    "-o", "ForwardX11=no",
    "-o", "ClearAllForwardings=yes",
    "-o", "PermitLocalCommand=no",
    "-o", "UpdateHostKeys=no",
]


def check_opts(spec: ConnSpec) -> list[str]:
    """ssh options that ask what a login would make of the server's key, without one.

    Run with ``-v``, ssh says whether the key it is offered is known and
    matches, is unknown, or differs from a known one, judged against exactly
    the known_hosts files the master uses. Strict checking, so nothing is
    recorded. The cluster's ssh config applies (``-F``, the alias's ProxyJump;
    a jump host logs in as configured).
    """
    return [
        *_NO_LOGIN,
        "-o", "StrictHostKeyChecking=yes",
        *_known_hosts_opts(spec),
        *_config_opts(spec),
    ]


def scan_opts(spec: ConnSpec, algorithm: Optional[str] = None) -> list[str]:
    """ssh options that fetch the server's host key without logging in.

    The key is recorded in the known_hosts file the caller adds with
    ``UserKnownHostsFile``, in plain form and always (the global file is not
    consulted). ``algorithm`` (``HostKeyAlgorithms``) asks for the key a login
    negotiates, which :func:`check_opts` reports.
    """
    return [
        *_NO_LOGIN,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "HashKnownHosts=no",
        "-o", "CheckHostIP=no",
        *(["-o", f"HostKeyAlgorithms={algorithm}"] if algorithm else []),
        *_config_opts(spec),
    ]


def check_argv(spec: ConnSpec) -> list[str]:
    """argv that asks the master whether it is alive (``ssh -O check``)."""
    return ["ssh", *_socket_opts(spec), "-O", "check", spec.target]


# Runs inside WSL: $1 is the socket path (may start with `~`), the rest is the
# `ssh -O check` argv. Child commands read /dev/null, not this script on stdin.
_PREPARE_SOCKET = r"""
p=$1; shift
case $p in "~"/*) p="$HOME/${p#??}" ;; esac
mkdir -p "$(dirname "$p")" || exit 1
if [ -S "$p" ] && ! "$@" </dev/null >/dev/null 2>&1; then rm -f "$p"; fi
"""


def prepare_socket(spec: ConnSpec) -> None:
    """Create the socket's directory and drop a stale socket before a new master.

    A dead socket file (left by a master that exited uncleanly) makes ssh refuse
    to multiplex ("ControlSocket ... already exists"); drop it if it isn't live.
    On Windows the socket lives inside WSL, so the same steps run there, and a
    failure raises :class:`wsl.WslError`.
    """
    if wsl.enabled():
        try:
            res = wsl.run(
                ["sh", "-s", "--", str(spec.socket), *check_argv(spec)],
                spec.wsl_distro,
                stdin=_PREPARE_SOCKET.encode("utf-8"),
                timeout=wsl.QUICK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise wsl.WslError("WSL did not respond while preparing the control socket.") from None
        if res.returncode != 0:
            raise wsl.WslError(f"cannot prepare the control socket {spec.socket} in WSL: {failure(res)}")
        return
    socket = Path(spec.socket)
    socket.parent.mkdir(parents=True, exist_ok=True)
    if socket.exists() and not is_live(spec):
        socket.unlink(missing_ok=True)


def open_master(spec: ConnSpec, verbose: int = 0) -> int:
    """Run the interactive master ssh in the current TTY; return its exit code.

    `-f` backgrounds the master after authentication, so this returns promptly
    once the OTP has been entered (or immediately for key-based auth). With
    ``verbose`` > 0, ssh's diagnostics stream straight to the current terminal.
    """
    prepare_socket(spec)
    argv = open_master_argv(spec, verbose)
    if wsl.enabled():
        # wsl.exe makes ssh the session leader of a pty. When `ssh -f` backgrounds
        # itself, the exiting parent's hangup can kill the new master before it
        # leaves the session (see pty_login), so start ssh with SIGHUP ignored.
        argv = ["sh", "-c", 'trap "" HUP; exec "$@"', "sh", *argv]
    return subprocess.run(wsl.launch(argv, spec.wsl_distro)).returncode


def captured(
    spec: ConnSpec,
    argv: list[str],
    stdin: Optional[IO[bytes]] = None,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run ``argv`` (through WSL on Windows) with its output captured as text.

    Output is decoded as UTF-8 explicitly: the platform default (cp1252 on most
    Windows machines) would garble or reject non-ASCII text from the cluster. A
    missing launcher (no ssh, or no wsl.exe on Windows) comes back as exit code
    127, and a ``timeout`` as exit code 124, instead of an exception. Without a
    ``stdin`` the command reads nothing, so it can't take the caller's input.
    """
    full = wsl.launch(argv, spec.wsl_distro)
    try:
        return subprocess.run(
            full,
            stdin=subprocess.DEVNULL if stdin is None else stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(full, 127, "", f"{full[0]}: {exc.strerror or exc}\n")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(full, 124, "", f"{full[0]}: no answer after {timeout}s\n")


def failure(res: subprocess.CompletedProcess) -> str:
    """Why ``res`` failed: what it said on stderr, or its exit code when it said nothing."""
    return (res.stderr or "").strip() or f"exit code {res.returncode}"


def check(spec: ConnSpec) -> subprocess.CompletedProcess:
    """Probe the master via ``ssh -O check``, capturing its output.

    Bounded by a timeout, so a WSL that hangs on Windows reads as "not live"
    instead of freezing ``status``, ``info`` or ``run``.
    """
    return captured(spec, check_argv(spec), timeout=wsl.QUICK_TIMEOUT)


def is_live(spec: ConnSpec) -> bool:
    """True if the master connection is alive (`ssh -O check`)."""
    return check(spec).returncode == 0


def _command_argv(spec: ConnSpec, remote: str, *, tty: bool = False) -> list[str]:
    """The ssh argv that runs the shell command line ``remote`` over the live tunnel."""
    return ["ssh", *_socket_opts(spec), "-o", "BatchMode=yes", *(["-tt"] if tty else []), spec.target, remote]


def run(
    spec: ConnSpec,
    tokens: list[str],
    *,
    tty: bool = False,
    on_line: Optional[Callable[[str], None]] = None,
) -> int:
    """Run a command over the live tunnel, streaming I/O; return remote exit code.

    With ``on_line`` (and no ``tty``), stdout passes through this process on its
    way to ours, and ``on_line`` sees each of its lines as it arrives (the first
    :data:`_SCAN_LINE_BYTES` of a longer one); nothing else of the output is
    kept. stdin and stderr always go straight to ssh.
    """
    argv = wsl.launch(_command_argv(spec, shlex.join(tokens), tty=tty), spec.wsl_distro)
    if tty or on_line is None:
        return subprocess.run(argv).returncode
    return _run_scanning(argv, on_line)


#: How much of each streamed line :func:`run` hands to ``on_line``, so a huge
#: line without a newline can't fill the memory.
_SCAN_LINE_BYTES = 4096


def _run_scanning(argv: list[str], on_line: Callable[[str], None]) -> int:
    write = _stdout_writer()
    pending = b""
    with subprocess.Popen(argv, stdout=subprocess.PIPE) as proc:
        assert proc.stdout is not None
        while chunk := proc.stdout.read1(65536):
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                on_line(line[:_SCAN_LINE_BYTES].decode("utf-8", "replace"))
            pending = pending[:_SCAN_LINE_BYTES]
            if write is None:
                continue
            try:
                write(chunk)
            except OSError:
                # Our stdout's reader is gone (`| head`): stop copying and close the
                # pipe, so ssh meets the closed pipe at its next write, as without ctun.
                _silence_stdout()
                break
        else:
            if pending:
                on_line(pending.decode("utf-8", "replace"))
        proc.stdout.close()
        return proc.wait()


def _stdout_writer() -> Optional[Callable[[bytes], None]]:
    """Writes streamed output to ``sys.stdout``: its bytes, or text when it has none; ``None`` without one."""
    stream = sys.stdout
    if stream is None:  # pythonw.exe
        return None
    stream.flush()
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:

        def write_bytes(chunk: bytes) -> None:
            buffer.write(chunk)
            buffer.flush()

        return write_bytes
    # A text-only stand-in, e.g. `contextlib.redirect_stdout(io.StringIO())`.
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def write_text(chunk: bytes) -> None:
        stream.write(decoder.decode(chunk))
        stream.flush()

    return write_text


def _silence_stdout() -> None:
    """Point a broken stdout at devnull, so Python's flush at exit doesn't fail on it."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (AttributeError, OSError, ValueError):
        pass  # a stand-in without a file descriptor has nothing to flush at exit


def capture(
    spec: ConnSpec, tokens: list[str], timeout: Optional[float] = None
) -> subprocess.CompletedProcess:
    """Run a command over the tunnel and capture its output (no streaming).

    After ``timeout`` seconds the local ssh is stopped and exit code 124 comes
    back; the remote command may keep running on the cluster.
    """
    return captured(spec, _command_argv(spec, shlex.join(tokens)), timeout=timeout)


def close(spec: ConnSpec) -> bool:
    """Cleanly tear down the master (`ssh -O exit`)."""
    argv = ["ssh", *_socket_opts(spec), "-O", "exit", spec.target]
    return captured(spec, argv, timeout=wsl.QUICK_TIMEOUT).returncode == 0


def feed_script(
    spec: ConnSpec, script_path: Path, args: list[object], timeout: Optional[float] = None
) -> subprocess.CompletedProcess:
    """Ship a local script over the tunnel and run it via `bash -s -- <args>`.

    The script is streamed on stdin (never written to the remote) and its
    positional ``args`` are shell-quoted, so nothing installed on the cluster is
    required beyond ``bash``. stdout/stderr are captured for the caller to
    interpret. This is the shared transport for every ctun probe (budget usage,
    job overview, ...). ``timeout`` works as in :func:`capture`.
    """
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    remote = f"bash -s -- {quoted}" if quoted else "bash -s"
    with open(script_path, "rb") as fh:
        return captured(
            spec,
            _command_argv(spec, remote),
            stdin=fh,
            timeout=timeout,
        )


def probe(
    spec: ConnSpec, script_path: Path, start_epoch: int, user: str
) -> subprocess.CompletedProcess:
    """Run a budget script on the login node, fed over the tunnel via `bash -s`.

    Passes positional args ``<start_epoch> <cluster> <user>`` to the script and
    captures its stdout/stderr. The caller interprets the result.
    """
    return feed_script(
        spec, script_path, [start_epoch, spec.name, user], timeout=constants.PROBE_TIMEOUT
    )
