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


# A stand-in for `ssh -M`: asks for a password, then an OTP, and on the right
# pair ("s3cret", "123456") creates a marker file standing in for the control
# socket. One line, so it survives any command-line quoting.
FAKE_MASTER = (
    "import pathlib, sys; "
    "pw = input('user@host password: '); otp = input('Verification code: '); "
    "ok = (pw, otp) == ('s3cret', '123456'); "
    "ok and pathlib.Path(sys.argv[1]).touch(); "
    "sys.exit(0 if ok else 1)"
)
FAKE_CHECK = "import os, sys; sys.exit(0 if os.path.exists(sys.argv[1]) else 255)"


@pytest.fixture
def fake_master(tmp_path: Path) -> tuple[list[str], list[str], Path]:
    """``(master_argv, check_argv, marker)`` for a fake ssh master."""
    marker = tmp_path / "socket"
    return (
        [sys.executable, "-c", FAKE_MASTER, str(marker)],
        [sys.executable, "-c", FAKE_CHECK, str(marker)],
        marker,
    )
