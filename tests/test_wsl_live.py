"""Opt-in checks against a real WSL install; they need no cluster or SSH server.

On Windows with WSL 2, run:

    $env:CTUN_WSL_TESTS = "1"; uv run pytest tests/test_wsl_live.py

Set ``CTUN_WSL_DISTRO`` to test a distro other than the default one.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import PurePosixPath

import pytest

from cluster_tunnel import popup, wsl
from cluster_tunnel.ssh import ConnSpec

from .conftest import FAKE_MASTER

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "win32" or os.environ.get("CTUN_WSL_TESTS") != "1",
        reason="real-WSL checks: Windows with CTUN_WSL_TESTS=1",
    ),
    pytest.mark.real_wsl,
]

DISTRO = os.environ.get("CTUN_WSL_DISTRO") or None


def test_preflight_passes() -> None:
    result = wsl.preflight(DISTRO)
    assert result.ok, result.problems


def test_arguments_arrive_verbatim() -> None:
    # Windows command line -> wsl.exe -> Linux argv must round-trip.
    args = ["a b", 'q"uote', "trailing\\", "", "$HOME", "~", "semi;colon"]
    res = wsl.run(
        ["python3", "-I", "-c", "import json, sys; print(json.dumps(sys.argv[1:]))", *args],
        DISTRO,
    )
    assert json.loads(res.stdout) == args


def test_stdin_arrives_without_carriage_returns() -> None:
    res = wsl.run(
        ["python3", "-I", "-c", "import sys; print(repr(sys.stdin.buffer.read()))"],
        DISTRO, stdin=b"a\nb\n",
    )
    assert res.stdout.strip() == repr(b"a\nb\n")


def test_to_wsl_path_round_trip(tmp_path) -> None:
    path = wsl.to_wsl_path(str(tmp_path) + "\\", DISTRO)
    assert path.startswith("/") and path.endswith("/")
    assert wsl.run(["test", "-d", path], DISTRO).returncode == 0


def test_login_driver_runs_inside_wsl() -> None:
    # What `login -i` does, with a fake master in place of ssh.
    marker = f"/tmp/ctun-wsl-test-{uuid.uuid4().hex}"
    master = ["python3", "-c", FAKE_MASTER, marker]
    check = ["test", "-e", marker]
    spec = ConnSpec("k", "u@h", PurePosixPath("~/x/k"), "12h", 60, 3, None, wsl_distro=DISTRO)
    try:
        assert popup._login_in_wsl(spec, master, check, "s3cret", "123456", 30, 1)
    finally:
        wsl.run(["rm", "-f", marker], DISTRO)
