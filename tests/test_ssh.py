"""SSH ControlMaster argv construction (no real ssh invoked)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cluster_tunnel import config as cfg
from cluster_tunnel import ssh, wsl
from cluster_tunnel.ssh import ConnSpec


def _config(tmp_path: Path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return cfg.load_config(str(p))


def test_conn_spec(tmp_path: Path) -> None:
    c = _config(
        tmp_path,
        "defaults:\n  control_persist: '8h'\nclusters:\n  k:\n    host: hh\n    user: uu\n",
    )
    spec = ssh.conn_spec(c, "k")
    assert spec.target == "uu@hh"
    assert spec.control_persist == "8h"
    assert spec.socket.name == "k"


def test_open_master_argv(tmp_path: Path) -> None:
    c = _config(tmp_path, "clusters:\n  k:\n    host: hh\n    ssh_alias: al\n")
    spec = ssh.conn_spec(c, "k")
    argv = ssh.open_master_argv(spec)
    assert argv[0] == "ssh"
    assert {"-M", "-N", "-f"} <= set(argv)
    assert argv[-1] == "al"
    assert any(a.startswith("ControlPersist=") for a in argv)


def test_run_argv(tmp_path: Path, monkeypatch) -> None:
    c = _config(tmp_path, "clusters:\n  k:\n    host: hh\n")
    spec = ssh.conn_spec(c, "k")
    captured: dict = {}

    def fake_run(argv, *a, **k):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    rc = ssh.run(spec, ["squeue", "--me"])
    assert rc == 0
    assert captured["argv"][-1] == "squeue --me"
    assert "BatchMode=yes" in captured["argv"]


def test_ssh_config_reaches_every_ssh_call(tmp_path: Path) -> None:
    c = _config(
        tmp_path,
        "defaults:\n  ssh_config: /etc/ctun/ssh_config\n"
        "clusters:\n  k:\n    host: hh\n  j:\n    host: jj\n    ssh_config: /other/config\n",
    )
    spec = ssh.conn_spec(c, "k")
    expected = str(Path("/etc/ctun/ssh_config"))  # native separators off WSL
    for argv in (ssh.open_master_argv(spec), ssh.check_argv(spec), ssh.control_opts(spec)):
        assert argv[argv.index("-F") + 1] == expected
    assert ssh.conn_spec(c, "j").ssh_config == "/other/config"  # per-cluster override


def test_windows_spec_uses_wsl_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    c = _config(
        tmp_path,
        "defaults:\n  wsl_distro: Ubuntu\n"
        "clusters:\n  k:\n    host: hh\n    identity_file: ~/.ssh/id_k\n",
    )
    spec = ssh.conn_spec(c, "k")
    assert str(spec.socket) == "~/.cache/cluster-tunnel/sockets/k"
    assert spec.wsl_distro == "Ubuntu"
    argv = ssh.open_master_argv(spec)
    assert argv[argv.index("-S") + 1] == "~/.cache/cluster-tunnel/sockets/k"
    assert argv[argv.index("-i") + 1] == "~/.ssh/id_k"  # WSL's ssh expands ~ itself


def test_windows_run_goes_through_wsl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    c = _config(tmp_path, "defaults:\n  wsl_distro: Ubuntu\nclusters:\n  k:\n    host: hh\n")
    spec = ssh.conn_spec(c, "k")
    captured: dict = {}

    def fake_run(argv, *a, **k):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ssh.run(spec, ["squeue", "--me"]) == 0
    assert captured["argv"][:5] == ["wsl.exe", "-d", "Ubuntu", "-e", "ssh"]
    assert captured["argv"][-1] == "squeue --me"


def test_windows_prepare_socket_runs_inside_wsl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    spec = ssh.conn_spec(_config(tmp_path, "clusters:\n  k:\n    host: hh\n"), "k")
    seen: dict = {}

    def fake_run(argv, distro=None, *, stdin=b"", timeout=None):
        seen.update(argv=argv, stdin=stdin)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(wsl, "run", fake_run)
    ssh.prepare_socket(spec)
    assert seen["argv"][:4] == ["sh", "-s", "--", "~/.cache/cluster-tunnel/sockets/k"]
    assert seen["argv"][4:] == ssh.check_argv(spec)
    assert seen["stdin"] == ssh._PREPARE_SOCKET.encode("utf-8")


def test_prepare_socket_creates_missing_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: False)
    monkeypatch.setattr(ssh, "is_live", lambda spec: False)
    spec = ConnSpec("k", "u@h", tmp_path / "a" / "b" / "k", "12h", 60, 3, None)
    ssh.prepare_socket(spec)
    assert (tmp_path / "a" / "b").is_dir()


def test_missing_launcher_reads_as_not_live(tmp_path: Path, monkeypatch) -> None:
    # No ssh (or no wsl.exe on Windows): "not live" instead of a traceback.
    spec = ssh.conn_spec(_config(tmp_path, "clusters:\n  k:\n    host: hh\n"), "k")

    def missing(argv, *a, **k):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(subprocess, "run", missing)
    assert ssh.check(spec).returncode == 127
    assert not ssh.is_live(spec)


def test_windows_prepare_socket_failure_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    spec = ssh.conn_spec(_config(tmp_path, "clusters:\n  k:\n    host: hh\n"), "k")
    monkeypatch.setattr(
        wsl, "run",
        lambda argv, distro=None, **k: subprocess.CompletedProcess(argv, 1, "", "mkdir: Permission denied"),
    )
    with pytest.raises(wsl.WslError, match="Permission denied"):
        ssh.prepare_socket(spec)


def test_check_timeout_reads_as_not_live(tmp_path: Path, monkeypatch) -> None:
    # A hung WSL must not freeze status/info/run.
    spec = ssh.conn_spec(_config(tmp_path, "clusters:\n  k:\n    host: hh\n"), "k")

    def hang(argv, *a, **k):
        raise subprocess.TimeoutExpired(argv, k.get("timeout"))

    monkeypatch.setattr(subprocess, "run", hang)
    assert ssh.check(spec).returncode == 124
    assert not ssh.is_live(spec)


def test_windows_open_master_starts_ssh_with_sighup_ignored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    monkeypatch.setattr(ssh, "prepare_socket", lambda spec: None)
    spec = ssh.conn_spec(_config(tmp_path, "clusters:\n  k:\n    host: hh\n"), "k")
    captured: dict = {}

    def fake_run(argv, *a, **k):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ssh.open_master(spec) == 0
    argv = captured["argv"]
    assert argv[:6] == ["wsl.exe", "-e", "sh", "-c", 'trap "" HUP; exec "$@"', "sh"]
    assert argv[6:] == ssh.open_master_argv(spec)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")
def test_sighup_wrapper_survives_exec() -> None:
    # The program exec'd by the wrapper starts with SIGHUP ignored.
    probe = "import signal; print(signal.getsignal(signal.SIGHUP) == signal.SIG_IGN)"
    res = subprocess.run(
        ["sh", "-c", 'trap "" HUP; exec "$@"', "sh", sys.executable, "-c", probe],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert res.stdout.strip() == "True", res.stderr
