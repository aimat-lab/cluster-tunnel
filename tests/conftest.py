"""Shared test fixtures."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from cluster_tunnel import wsl


@pytest.fixture(autouse=True)
def _native_transport(request, monkeypatch) -> None:
    """Run every test on the native (non-WSL) transport, on every OS.

    On the Windows CI runner ``wsl.enabled()`` is really true, but there is no
    usable WSL there. Tests of the WSL path switch it on themselves; tests
    marked ``real_wsl`` keep the real behaviour.
    """
    if request.node.get_closest_marker("real_wsl") is None:
        monkeypatch.setattr(wsl, "enabled", lambda: False)
        monkeypatch.setattr(wsl, "_wsl_exe", lambda: "wsl.exe")


def _find_bash() -> str | None:
    """A bash that runs the bundled .sh scripts with this OS's native paths.

    On Windows a bare ``bash`` can resolve to System32's WSL launcher, which runs
    inside WSL and cannot see Windows paths, so Git for Windows' bash is used.
    """
    if sys.platform != "win32":
        return shutil.which("bash")
    candidates: list[Path] = []
    git = shutil.which("git")
    if git:
        # <Git>\cmd\git.exe or <Git>\mingw64\bin\git.exe -> <Git>\bin\bash.exe
        for root in Path(git).resolve().parents[:3]:
            candidates.append(root / "bin" / "bash.exe")
    found = shutil.which("bash")
    if found:
        candidates.append(Path(found))
    for cand in candidates:
        low = str(cand).lower()
        if cand.is_file() and "system32" not in low and "windowsapps" not in low:
            return str(cand)
    return None


@pytest.fixture
def bash() -> str:
    """Absolute path to a usable bash; skips the test when there is none."""
    path = _find_bash()
    if path is None:
        pytest.skip("needs a POSIX bash (on Windows: Git for Windows)")
    return path


# A stand-in for `ssh -M ... -f`, faithful where the login driver has broken:
# - a pre-auth banner that mentions "password" (a line, not a prompt);
# - input typed before a prompt is dropped, as ssh's readpassphrase does;
# - a wrong OTP is asked for again;
# - `-E <file>` takes the -v debug lines, which also mention "password";
# - on success it backgrounds like `ssh -f`: the session leader exits at once and
#   the child leaves the session only after a delay, so an unignored SIGHUP from
#   the hangup kills it before it creates the marker (the "control socket").
FAKE_SSH = r"""
import os, sys, termios, time

args = sys.argv[1:]
log = None
if args[:1] == ["-E"]:
    log, args = args[1], args[2:]
marker = args[0]

print("Notice: have your password and OTP ready.", flush=True)
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("debug1: Authentications that can continue: publickey,password\n")


def ask(prompt):
    time.sleep(0.2)
    termios.tcflush(0, termios.TCIFLUSH)
    return input(prompt)


if ask("user@host's password: ") != "s3cret":
    sys.exit(1)
for _ in range(3):
    if ask("Verification code: ") == "123456":
        break
else:
    sys.exit(1)

if os.fork():
    os._exit(0)
time.sleep(0.3)
os.setsid()
open(marker, "w").close()
"""

# One-line variant for running inside WSL (tests/test_wsl_live.py): prompts, then
# backgrounds like `ssh -f`, so it exercises the same SIGHUP race there.
FAKE_MASTER = (
    "import os, pathlib, sys, time; "
    "pw = input('user@host password: '); otp = input('Verification code: '); "
    "(pw, otp) == ('s3cret', '123456') or sys.exit(1); "
    "os.fork() and os._exit(0); time.sleep(0.3); os.setsid(); "
    "pathlib.Path(sys.argv[1]).touch()"
)
FAKE_CHECK = "import os, sys; sys.exit(0 if os.path.exists(sys.argv[1]) else 255)"


@pytest.fixture
def fake_master(tmp_path: Path) -> tuple[list[str], list[str], Path]:
    """``(master_argv, check_argv, marker)`` for a fake ssh master (Linux/macOS)."""
    script = tmp_path / "fake_ssh"
    script.write_text(f"#!{sys.executable}\n{FAKE_SSH}", encoding="utf-8")
    script.chmod(0o755)
    marker = tmp_path / "socket"
    return (
        [str(script), str(marker)],
        [sys.executable, "-c", FAKE_CHECK, str(marker)],
        marker,
    )
