"""The WSL bridge with wsl.exe faked, so these run on any OS."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from cluster_tunnel import ssh, wsl

posix_sh = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None, reason="needs a POSIX sh"
)

# The real lookup, captured before conftest's autouse fixture stubs it per test.
REAL_WSL_EXE = wsl._wsl_exe


def _fake_wsl(monkeypatch, stdout: str = "", rc: int = 0, stderr: str = "") -> list:
    """Replace wsl.run with a stub returning the given result; returns its calls."""
    calls: list = []

    def fake(argv, distro=None, **kwargs):
        calls.append((argv, distro))
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)

    monkeypatch.setattr(wsl, "run", fake)
    return calls


@pytest.mark.real_wsl
def test_enabled_follows_platform(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert wsl.enabled()
    monkeypatch.setattr(sys, "platform", "linux")
    assert not wsl.enabled()


def test_wrap_with_and_without_distro() -> None:
    assert wsl.wrap(["ssh", "-V"]) == ["wsl.exe", "-e", "ssh", "-V"]
    assert wsl.wrap(["ssh"], "Ubuntu") == ["wsl.exe", "-d", "Ubuntu", "-e", "ssh"]


def test_wsl_exe_is_an_absolute_path(monkeypatch) -> None:
    # Never a bare "wsl.exe": Windows would search the working directory first.
    present = {"C:\\Windows\\System32\\wsl.exe"}
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    monkeypatch.setattr(os.path, "isfile", lambda path: path in present)
    assert REAL_WSL_EXE() == "C:\\Windows\\System32\\wsl.exe"
    present.add("C:\\Windows\\Sysnative\\wsl.exe")  # what a 32-bit Python must use
    assert REAL_WSL_EXE() == "C:\\Windows\\Sysnative\\wsl.exe"
    present.clear()
    assert REAL_WSL_EXE() == "wsl.exe"  # WSL not installed: let the launch fail clearly


def test_launch_wraps_only_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: False)
    assert wsl.launch(["ssh", "x"], "Ubuntu") == ["ssh", "x"]
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    assert wsl.launch(["ssh", "x"], "Ubuntu") == ["wsl.exe", "-d", "Ubuntu", "-e", "ssh", "x"]


def test_run_feeds_stdin_as_bytes(monkeypatch) -> None:
    # A text-mode pipe would turn \n into \r\n on Windows and break the scripts.
    captured: dict = {}

    def fake(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "é\n".encode("utf-8"), b"")

    monkeypatch.setattr(subprocess, "run", fake)
    res = wsl.run(["sh", "-s"], stdin=b"echo hi\n")
    assert captured["input"] == b"echo hi\n" and "text" not in captured
    assert res.stdout == "é\n"


# --- to_wsl_path ---------------------------------------------------------------


def _fake_wslpath(monkeypatch) -> list:
    """Stub wsl.run as a wslpath that maps C:\\... to /mnt/c/...; returns its calls."""
    calls: list = []

    def fake(argv, distro=None, **kwargs):
        calls.append((argv, distro))
        windows = argv[-1]
        return subprocess.CompletedProcess(
            argv, 0, "/mnt/c/" + windows[3:].replace("\\", "/") + "\n", ""
        )

    monkeypatch.setattr(wsl, "run", fake)
    return calls


def test_relative_path_is_resolved_against_the_working_directory(monkeypatch) -> None:
    # Made absolute first, so an untranslatable working directory (UNC) fails
    # loudly instead of WSL quietly starting somewhere else.
    calls = _fake_wslpath(monkeypatch)
    monkeypatch.setattr(os, "getcwd", lambda: "C:\\proj")
    assert wsl.to_wsl_path("data\\sub") == "/mnt/c/proj/data/sub"
    assert wsl.to_wsl_path("data\\") == "/mnt/c/proj/data/"  # trailing slash kept
    assert wsl.to_wsl_path("./a/b/") == "/mnt/c/proj/a/b/"
    assert [argv[-1] for argv, _ in calls] == [
        "C:\\proj\\data\\sub", "C:\\proj\\data", "C:\\proj\\a\\b"
    ]


def test_wsl_path_passes_through(monkeypatch) -> None:
    calls = _fake_wsl(monkeypatch)
    assert wsl.to_wsl_path("/home/me/data/") == "/home/me/data/"
    assert calls == []


def test_windows_path_goes_through_wslpath_keeping_trailing_slash(monkeypatch) -> None:
    calls = _fake_wslpath(monkeypatch)
    assert wsl.to_wsl_path("C:\\Users\\me\\data\\", "Ubuntu") == "/mnt/c/Users/me/data/"
    assert calls == [(["wslpath", "-a", "C:\\Users\\me\\data"], "Ubuntu")]


def test_wslpath_failure_raises(monkeypatch) -> None:
    _fake_wsl(monkeypatch, rc=1, stderr="wslpath: Z:\\nope: Invalid argument")
    with pytest.raises(wsl.WslError, match="Invalid argument"):
        wsl.to_wsl_path("Z:\\nope")


def test_wslpath_timeout_raises(monkeypatch) -> None:
    def hang(argv, distro=None, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(wsl, "run", hang)
    with pytest.raises(wsl.WslError, match="did not respond"):
        wsl.to_wsl_path("C:\\data")


# --- preflight -----------------------------------------------------------------


def test_preflight_ok_names_the_distro(monkeypatch) -> None:
    _fake_wsl(monkeypatch, stdout="distro=Ubuntu-24.04\n")
    result = wsl.preflight()
    assert result.ok and result.distro == "Ubuntu-24.04"


def test_preflight_reports_missing_tools(monkeypatch) -> None:
    _fake_wsl(monkeypatch, stdout="distro=Debian\nmissing=rsync\nmissing=python3\n")
    result = wsl.preflight()
    text = " ".join(result.problems)
    assert not result.ok
    assert "'Debian' lacks rsync, python3" in text and "wsl_distro" in text


def test_preflight_rejects_wsl1_and_old_python(monkeypatch) -> None:
    _fake_wsl(monkeypatch, stdout="distro=Legacy\nwsl1\npython=3.8.10\n")
    text = " ".join(wsl.preflight().problems)
    assert "WSL 1" in text and "3.8.10" in text


def test_preflight_without_wsl(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "wsl.exe")

    monkeypatch.setattr(wsl, "run", missing)
    result = wsl.preflight()
    assert not result.ok and "not installed" in result.problems[0]


def test_preflight_distro_that_cannot_start(monkeypatch) -> None:
    _fake_wsl(monkeypatch, rc=1, stderr="There is no distribution with the supplied name.")
    problem = wsl.preflight("Nope").problems[0]
    assert "'Nope'" in problem and "no distribution" in problem


# --- the shell scripts, run by a real local sh ------------------------------------


@posix_sh
def test_preflight_script_reports_facts() -> None:
    res = subprocess.run(["sh", "-s"], input=wsl._PREFLIGHT.encode("utf-8"), capture_output=True)
    assert res.returncode == 0, res.stderr
    assert "distro=" in res.stdout.decode("utf-8")


@posix_sh
def test_prepare_socket_script() -> None:
    # `~` is expanded, missing dirs are created, and a stale socket goes only
    # when the check fails. A short /tmp home: macOS caps socket paths at 104 bytes.
    home = Path(tempfile.mkdtemp(prefix="ctun", dir="/tmp"))
    env = dict(os.environ, HOME=str(home))

    def prepare(path: str, check: str) -> None:
        res = subprocess.run(
            ["sh", "-s", "--", path, check],
            input=ssh._PREPARE_SOCKET.encode("utf-8"), env=env, capture_output=True,
        )
        assert res.returncode == 0, res.stderr

    try:
        prepare("~/s/new/k", "true")
        assert (home / "s" / "new").is_dir()

        stale = home / "s" / "k"
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(stale))
        sock.close()
        prepare("~/s/k", "true")  # master answers: keep the socket
        assert stale.exists()
        prepare("~/s/k", "false")  # master is gone: drop the stale socket
        assert not stale.exists()
    finally:
        shutil.rmtree(home, ignore_errors=True)
