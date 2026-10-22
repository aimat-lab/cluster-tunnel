"""Host-key pinning: what a login makes of a cluster's host key, and pinning it.

A tunnel's master checks the server's key as its cluster says:
``strict_host_key_checking: accept-new`` (the default) takes an unknown key on
first contact and refuses a changed one; ``'yes'`` accepts only a known key. A
cluster is pinned when its ``known_hosts_file`` is set: that file is then the
only list of trusted keys (ssh's own files and DNS are not consulted).

:func:`check` asks ssh itself what a login would make of the key the server
presents now, judged against exactly the files the master uses; :func:`scan`
fetches that key in known_hosts form, to pin it. Neither logs in. The key file
is read and written where ssh runs (inside WSL on Windows), by one POSIX sh
helper fed on stdin, with ``ssh-keygen -F``/``-R`` so hashed entries count too.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cluster_tunnel import ssh, wsl
from cluster_tunnel.ssh import ConnSpec

#: Seconds to wait for a connection or the helper; covers a cold WSL start.
TIMEOUT = 90

# Operations (the first argument):
#   scan <target> <ssh option>...  fetch the target's host key into a throwaway
#       known_hosts file; print its lines, a "--" line, then their fingerprints.
#   pinned <file> <host>  the same for the keys <file> holds for <host>.
#   pin <file> <host> <line>...  replace <file>'s keys for <host> with <line>s.
# A leading "~/" in <file> means $HOME, as in the config. Child commands read
# /dev/null, not this script on stdin.
_HELPER = r"""
expand() { case $1 in "~"/*) printf '%s\n' "$HOME/${1#??}" ;; *) printf '%s\n' "$1" ;; esac; }
show() { cat "$1" && echo -- && ssh-keygen -l -f "$1" </dev/null; }
op=$1; shift
case $op in
scan)
    target=$1; shift
    d=$(mktemp -d) || exit 1
    trap 'rm -rf "$d"' EXIT
    ssh "$@" -o "UserKnownHostsFile=\"$d/known_hosts\"" "$target" true </dev/null >/dev/null 2>"$d/err"
    rc=$?
    if [ -s "$d/known_hosts" ]; then show "$d/known_hosts"; exit; fi
    cat "$d/err" >&2
    [ "$rc" -ne 0 ] && exit "$rc"
    echo "the server sent no host key" >&2
    exit 1
    ;;
pinned)
    f=$(expand "$1")
    [ -f "$f" ] || exit 0
    d=$(mktemp -d) || exit 1
    trap 'rm -rf "$d"' EXIT
    ssh-keygen -F "$2" -f "$f" </dev/null | grep -v '^#' > "$d/known_hosts"
    [ -s "$d/known_hosts" ] || exit 0
    show "$d/known_hosts"
    ;;
pin)
    f=$(expand "$1"); h=$2; shift 2
    mkdir -p "$(dirname "$f")" || exit 1
    if [ -f "$f" ]; then
        # ssh-keygen -R leaves the previous file as <file>.old; one that was there stays.
        [ -e "$f.old" ] && mv "$f.old" "$f.old.$$"
        ssh-keygen -R "$h" -f "$f" </dev/null >/dev/null 2>&1
        rc=$?
        rm -f "$f.old"
        [ -e "$f.old.$$" ] && mv "$f.old.$$" "$f.old"
        [ "$rc" -eq 0 ] || exit 1
        [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ] && echo >> "$f"  # end the last line
    fi
    printf '%s\n' "$@" >> "$f"
    ;;
*)
    echo "unknown operation: $op" >&2
    exit 2
    ;;
esac
"""


@dataclass
class HostKey:
    """One host key: its known_hosts line and its fingerprint."""

    host: str  # the name ssh looks the key up by: the host, [host]:port, or a HostKeyAlias
    kind: str  # ED25519, ECDSA, RSA, ...
    bits: int
    fingerprint: str  # SHA256:...
    line: str  # the known_hosts line, "<host> <key type> <base64 key>"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Check:
    """What a login would make of the key the server presents now."""

    trust: str  # "known" (it matches a trusted key), "new" (none is known) or "changed"
    algorithm: str  # the host-key algorithm negotiated, e.g. ssh-ed25519
    fingerprint: str  # SHA256:...
    conflicts: list[str] = field(default_factory=list)  # "file:line" of a differing known key


def default_file(config_path: Path) -> str:
    """Where pinned keys go unless a cluster names its own file: next to the config
    (a WSL path on Windows, where ssh runs). Absolute, as later logins may start
    in another directory."""
    if wsl.enabled():
        return wsl.DEFAULT_KNOWN_HOSTS
    return str(Path(config_path).expanduser().absolute().parent / "known_hosts")


def check(spec: ConnSpec) -> Check:
    """Ask ssh what a login to ``spec`` would make of the key the server presents.

    One connection with the master's own known_hosts choice but strict checking
    (nothing is recorded), stopping before authentication. Raises
    :class:`RuntimeError` with ssh's own words when the server can't be reached.
    """
    argv = ["ssh", "-v", *ssh.check_opts(spec), spec.target, "true"]
    return parse_check(ssh.captured(spec, argv, timeout=TIMEOUT).stderr)


#: A jump connection that logged in; its lines end here.
_JUMPED = re.compile(r"^(?:debug1: )?Authenticated to .*$", re.MULTILINE)
#: A proxy command that runs ``ssh -v``, as ProxyJump does under ``-v``.
_VERBOSE_PROXY = re.compile(r"Executing proxy command: .*\s-v\b")


def parse_check(stderr: str) -> Check:
    """Read ssh's verdict from its ``-v`` output.

    Behind a jump host, ssh passes ``-v`` on to the jump connection, whose lines
    (its own key, its own verdict) come first and end once it has logged in;
    the cluster's lines follow. The check itself never logs in.
    """
    jumps = list(_JUMPED.finditer(stderr))
    if jumps:
        stderr = stderr[jumps[-1].end():]
    elif _VERBOSE_PROXY.search(stderr):
        raise RuntimeError(f"could not get through the jump host: {_last_words(stderr)}")
    key = re.search(r"Server host key: (\S+) (SHA256:\S+)", stderr)
    algorithm = re.search(r"kex: host key algorithm: (\S+)", stderr)
    if not key:
        raise RuntimeError(_last_words(stderr) or "the server sent no host key")
    conflicts = [m.strip() for m in re.findall(r"Offending \S+ key in (.+)", stderr)]
    if "REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr:
        trust = "changed"
    elif re.search(r"is known and matches the \S+ host key", stderr):
        trust = "known"
    elif "host key is known for" in stderr or "Host key verification failed" in stderr:
        trust = "new"
    else:
        raise RuntimeError(f"could not tell whether the key is known: {_last_words(stderr)}")
    return Check(trust, algorithm.group(1) if algorithm else key.group(1), key.group(2), conflicts)


def scan(spec: ConnSpec, algorithm: str | None = None) -> list[HostKey]:
    """The host key ``spec``'s server presents, fetched without logging in.

    ``algorithm`` asks for the key a login negotiates (:attr:`Check.algorithm`).
    Raises :class:`RuntimeError` with ssh's own words when the server can't be
    reached or sends no key.
    """
    keys = parse(_checked(spec, ["scan", spec.target, *ssh.scan_opts(spec, algorithm)]))
    if not keys:
        raise RuntimeError("the server sent no host key")
    return keys


def pinned(spec: ConnSpec, file: str, host: str) -> list[HostKey]:
    """The keys ``file`` holds for ``host`` (hashed entries too); empty if none."""
    return parse(_checked(spec, ["pinned", file, host]))


def pin(spec: ConnSpec, file: str, keys: list[HostKey]) -> None:
    """Make ``keys`` the only keys ``file`` holds for their host."""
    hosts = {key.host for key in keys}
    if len(hosts) != 1:
        raise ValueError(f"pin the keys of one host at a time, not {sorted(hosts)}")
    _checked(spec, ["pin", file, hosts.pop(), *(key.line for key in keys)])


def parse(output: str) -> list[HostKey]:
    """Pair the helper's known_hosts lines with the fingerprints after its ``--`` line.

    ``ssh-keygen -l`` prints one line per key, in file order, e.g.
    ``256 SHA256:nThb... uc3.scc.kit.edu (ED25519)``.
    """
    if not output.strip():
        return []
    head, sep, tail = output.partition("\n--\n")
    if not sep:
        raise RuntimeError(f"unexpected output from the host-key helper: {output!r}")
    lines = [ln for ln in head.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    prints = [ln for ln in tail.splitlines() if ln.strip()]
    if len(lines) != len(prints):
        raise RuntimeError("could not match the fingerprints to the host keys")
    keys = []
    for line, printed in zip(lines, prints):
        bits, fingerprint = printed.split()[:2]
        kind = printed.rsplit("(", 1)[-1].rstrip(")") if printed.endswith(")") else "?"
        keys.append(HostKey(line.split()[0], kind, int(bits), fingerprint, line))
    return keys


def _last_words(stderr: str) -> str:
    """What ssh said, without its debug chatter."""
    said = [ln.strip() for ln in stderr.splitlines() if ln.strip() and not ln.startswith("debug")]
    return "; ".join(said[-3:])


def _checked(spec: ConnSpec, args: list[str]) -> str:
    """Run the helper with ``args`` where ssh runs; return its stdout or raise."""
    res = _run_helper(spec, args)
    if res.returncode != 0:
        raise RuntimeError(ssh.failure(res))
    return res.stdout


def _run_helper(spec: ConnSpec, args: list[str]) -> subprocess.CompletedProcess:
    """The helper's output as text. The script goes in as bytes: a text pipe on
    Windows turns ``\\n`` into ``\\r\\n``, which breaks it on the Linux side."""
    argv = wsl.launch(["sh", "-s", "--", *args], spec.wsl_distro)
    try:
        res = subprocess.run(
            argv, input=_HELPER.encode("utf-8"), capture_output=True, timeout=TIMEOUT
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: {exc.strerror or exc}\n")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"no answer after {TIMEOUT}s\n")
    return subprocess.CompletedProcess(
        argv, res.returncode, res.stdout.decode("utf-8", "replace"), res.stderr.decode("utf-8", "replace")
    )
