"""Config loading, validation, and resolution helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from cluster_tunnel import config as cfg
from cluster_tunnel.config import Cluster


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_load_and_defaults(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  c:\n    host: h\n    user: u\n")
    c = cfg.load_config(str(p))
    assert "c" in c.clusters
    assert c.defaults.socket_dir  # filled by the loader
    assert c.defaults.control_persist == "12h"


def test_resolve_target_variants() -> None:
    assert cfg.resolve_target(Cluster(ssh_alias="al", host="h", user="u")) == "al"
    assert cfg.resolve_target(Cluster(host="h", user="u")) == "u@h"
    assert cfg.resolve_target(Cluster(host="h")) == "h"


def test_budget_script_path(tmp_path: Path) -> None:
    cp = tmp_path / "config.yaml"
    assert cfg.budget_script_path(cp, "horeka", None) == tmp_path / "budget" / "horeka.sh"
    assert cfg.budget_script_path(cp, "horeka", "/abs/x.sh") == Path("/abs/x.sh")
    assert cfg.budget_script_path(cp, "horeka", "budget/custom.sh") == tmp_path / "budget" / "custom.sh"


def test_missing_host_or_alias(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  bad:\n    description: x\n")
    with pytest.raises(Exception):
        cfg.load_config(str(p))


def test_extra_forbidden(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  c:\n    host: h\n    nope: 1\n")
    with pytest.raises(Exception):
        cfg.load_config(str(p))


def test_bad_fail_mode(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  c:\n    host: h\n    budget:\n      fail_mode: maybe\n")
    with pytest.raises(Exception):
        cfg.load_config(str(p))


def test_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        cfg.load_config("/nonexistent/does-not-exist.yaml")
