"""Budget guard logic (ssh.probe and session.load are mocked)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from cluster_tunnel import budget
from cluster_tunnel import config as cfg
from cluster_tunnel.ssh import ConnSpec


def _spec() -> ConnSpec:
    return ConnSpec("k", "k", Path("/tmp/k"), "12h", 60, 3, None)


def _config_with_budget(tmp_path: Path, *, fail_mode="closed", script="x.sh", limit=100):
    body = (
        "clusters:\n  k:\n    host: h\n    user: u\n    budget:\n"
        f"      script: {script}\n      session_limit: {limit}\n      unit: core-hours\n"
        f"      fail_mode: {fail_mode}\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return cfg.load_config(str(p)), p


def _dummy_script(tmp_path: Path) -> str:
    s = tmp_path / "x.sh"
    s.write_text("#!/bin/sh\n")
    return str(s)


def _patch(monkeypatch, *, sess, probe_stdout=None, probe_rc=0):
    monkeypatch.setattr("cluster_tunnel.session.load", lambda name: sess)
    if probe_stdout is not None:
        cp = subprocess.CompletedProcess([], probe_rc, probe_stdout, "")
        monkeypatch.setattr("cluster_tunnel.ssh.probe", lambda *a, **k: cp)


def test_first_token_and_guarded() -> None:
    assert budget.first_token_name(["/usr/bin/sbatch", "x"]) == "sbatch"
    assert budget.is_guarded(["sbatch"], ["sbatch", "srun"]) is True
    assert budget.is_guarded(["squeue"], ["sbatch"]) is False
    assert budget.is_guarded([], ["sbatch"]) is False


def test_not_guarded(tmp_path: Path) -> None:
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    d = budget.decide(c, "k", _spec(), ["squeue", "--me"], p)
    assert d.allowed and "not a guarded" in d.reason


def test_over_budget(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"}, probe_stdout="150\n")
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert not d.allowed and d.used == 150.0


def test_under_budget(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"}, probe_stdout="5\n")
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert d.allowed and d.used == 5.0


def test_no_limit_allowed(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": None, "unit": "core-hours"})
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert d.allowed and "no session limit" in d.reason


def test_fail_closed(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, fail_mode="closed", script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"}, probe_stdout="", probe_rc=1)
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert not d.allowed and "fail-closed" in d.reason


def test_fail_open(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, fail_mode="open", script=_dummy_script(tmp_path))
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"}, probe_stdout="", probe_rc=1)
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert d.allowed and "fail-open" in d.reason


def test_script_missing(tmp_path: Path, monkeypatch) -> None:
    c, p = _config_with_budget(tmp_path, script="does_not_exist.sh")
    _patch(monkeypatch, sess={"start_epoch": 0, "limit": 100, "unit": "core-hours"})
    d = budget.decide(c, "k", _spec(), ["sbatch", "j"], p)
    assert not d.allowed and "not found" in d.reason
