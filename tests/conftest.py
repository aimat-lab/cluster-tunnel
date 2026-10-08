"""Shared test fixtures."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest


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
