"""SSH ControlMaster argv construction (no real ssh invoked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from cluster_tunnel import config as cfg
from cluster_tunnel import ssh


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
