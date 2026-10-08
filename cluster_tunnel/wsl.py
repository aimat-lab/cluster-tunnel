"""WSL bridge: how ctun runs its Linux transport on native Windows.

No OpenSSH build for Windows can serve sessions over a ControlMaster socket, so
on Windows ctun runs ``ssh`` and ``rsync`` inside WSL 2 via ``wsl.exe``. ctun
itself stays a Windows program; only the transport crosses over. The control
socket, the ssh config and the keys therefore live in WSL, and the config paths
that point at them (``socket_dir``, ``identity_file``, ``ssh_config``) are paths
inside WSL.
"""

from __future__ import annotations

import ntpath
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional, Sequence

#: Where control sockets live inside WSL; ssh expands the ``~`` itself.
DEFAULT_SOCKET_DIR = "~/.cache/cluster-tunnel/sockets"

#: Seconds to wait for a quick command through wsl.exe; covers a cold WSL start.
QUICK_TIMEOUT = 60


class WslError(RuntimeError):
    """WSL could not do what ctun needed, e.g. translate a path."""


def enabled() -> bool:
    """True when the transport runs through WSL, i.e. on native Windows."""
    return sys.platform == "win32"


def _wsl_exe() -> str:
    """Absolute path to wsl.exe.

    Windows looks for a bare program name in the working directory before
    System32, so a planted ``wsl.exe`` there would receive everything ctun sends,
    secrets included. ``Sysnative`` is the real System32 as a 32-bit Python sees it.
    """
    root = os.environ.get("SystemRoot", r"C:\Windows")
    for folder in ("Sysnative", "System32"):
        candidate = ntpath.join(root, folder, "wsl.exe")
        if os.path.isfile(candidate):
            return candidate
    return "wsl.exe"


def wrap(argv: Sequence[str], distro: Optional[str] = None) -> list[str]:
    """``argv`` run inside WSL: ``wsl.exe [-d DISTRO] -e ARGV...``.

    ``-e`` executes the program directly, with no Linux shell in between, so
    the arguments arrive verbatim.
    """
    prefix = [_wsl_exe(), "-d", distro] if distro else [_wsl_exe()]
    return [*prefix, "-e", *argv]


def launch(argv: Sequence[str], distro: Optional[str] = None) -> list[str]:
    """``argv`` as this machine must start it: through WSL on Windows, else as is."""
    return wrap(argv, distro) if enabled() else list(argv)


def run(
    argv: Sequence[str],
    distro: Optional[str] = None,
    *,
    stdin: bytes = b"",
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run ``argv`` inside WSL, feeding ``stdin``; output comes back as UTF-8 text.

    The input goes in as bytes on purpose: a text-mode pipe on Windows turns
    every ``\\n`` into ``\\r\\n``, which breaks shell scripts and Python source
    on the Linux side.
    """
    res = subprocess.run(wrap(argv, distro), input=stdin, capture_output=True, timeout=timeout)
    return subprocess.CompletedProcess(
        res.args,
        res.returncode,
        res.stdout.decode("utf-8", "replace"),
        res.stderr.decode("utf-8", "replace"),
    )


# Reports facts as `key=value` lines. Child commands read /dev/null, not the
# script on stdin. The 3.10 minimum matches the package's requires-python.
_PREFLIGHT = r"""
echo "distro=${WSL_DISTRO_NAME:-}"
case "$(uname -r)" in *-Microsoft) echo "wsl1" ;; esac
for tool in ssh rsync python3; do
  command -v "$tool" >/dev/null 2>&1 || echo "missing=$tool"
done
if command -v python3 >/dev/null 2>&1; then
  python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 10))' </dev/null \
    || echo "python=$(python3 -I -c 'import platform; print(platform.python_version())' </dev/null)"
fi
"""


@dataclass
class Preflight:
    """Outcome of :func:`preflight`: the distro that answered, and what is wrong."""

    distro: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def preflight(distro: Optional[str] = None) -> Preflight:
    """Check that WSL can carry ctun's transport.

    That needs WSL 2 and a distro with ssh, rsync and python3 3.10 or newer.
    Problems come back as actionable messages naming the distro; nothing raises.
    """
    label = f"WSL distro '{distro}'" if distro else "The default WSL distro"
    try:
        res = run(["sh", "-s"], distro, stdin=_PREFLIGHT.encode("utf-8"), timeout=120)
    except FileNotFoundError:
        return Preflight(
            distro or "",
            ["WSL is not installed. Install WSL 2 (`wsl --install` in an "
             "administrator PowerShell), then retry."],
        )
    except subprocess.TimeoutExpired:
        return Preflight(distro or "", [f"{label} did not respond within 2 minutes."])

    facts = [line.strip() for line in res.stdout.splitlines()]
    values = {f.split("=", 1)[0]: f.split("=", 1)[1] for f in facts if "=" in f}
    if "distro" not in values:
        detail = (res.stderr or res.stdout).strip() or f"wsl.exe exited with code {res.returncode}"
        return Preflight(distro or "", [f"{label} could not run a command: {detail}"])

    name = values["distro"] or distro or "default"
    who = f"WSL distro '{name}'"
    problems = []
    if "wsl1" in facts:
        problems.append(
            f"{who} runs under WSL 1, which ctun does not support; convert it "
            f"with `wsl --set-version {name} 2`."
        )
    missing = [f.split("=", 1)[1] for f in facts if f.startswith("missing=")]
    if missing:
        problems.append(
            f"{who} lacks {', '.join(missing)}; install inside WSL, e.g. "
            "`sudo apt install openssh-client rsync python3`."
        )
    if "python" in values:
        problems.append(f"{who} has python3 {values['python']}; ctun needs 3.10 or newer.")
    if problems:
        problems.append("To use another distro, set `wsl_distro` under `defaults` in ctun's config.")
    return Preflight(name, problems)


def to_wsl_path(path: str, distro: Optional[str] = None) -> str:
    """Translate a local path for a program that runs inside WSL (rsync).

    ``/...`` is already a WSL path and passes through. Anything else is made
    absolute against ctun's working directory and goes through ``wslpath``:
    WSL only starts in that directory when it can translate it (not on a UNC
    path, say), so relying on it could quietly use the wrong directory. A
    trailing separator is kept, since rsync copies a directory's contents for
    ``dir/`` but the directory itself for ``dir``.
    """
    if path.startswith("/"):
        return path
    trailing = path.endswith(("/", "\\"))
    absolute = ntpath.normpath(ntpath.join(os.getcwd(), path))
    try:
        res = run(["wslpath", "-a", absolute], distro, timeout=QUICK_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise WslError(f"WSL did not respond while translating '{path}'.") from None
    converted = res.stdout.strip()
    if res.returncode != 0 or not converted:
        detail = res.stderr.strip() or f"wslpath exited with code {res.returncode}"
        raise WslError(f"cannot translate '{path}' to a WSL path: {detail}")
    if trailing and not converted.endswith("/"):
        converted += "/"
    return converted
