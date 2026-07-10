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


def test_requires_password_defaults_true_and_overridable() -> None:
    # Default: a cluster uses a service password unless told otherwise.
    assert Cluster(host="h").requires_password is True
    assert Cluster(host="h", requires_password=False).requires_password is False


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


# --- validation_warnings -------------------------------------------------------


def test_warn_missing_budget_script(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "clusters:\n  c:\n    host: h\n    budget:\n      script: budget/nope.sh\n",
    )
    config = cfg.load_config(str(p))
    warnings = cfg.validation_warnings(config, p)
    assert len(warnings) == 1
    assert "budget script not found" in warnings[0]
    assert "nope.sh" in warnings[0]


def test_warn_invalid_guard_regex(tmp_path: Path) -> None:
    script = tmp_path / "x.sh"
    script.write_text("#!/bin/sh\n")
    p = _write(
        tmp_path,
        "clusters:\n  c:\n    host: h\n    budget:\n"
        "      script: x.sh\n      guard_commands: ['sbatch', 's(run']\n",
    )
    config = cfg.load_config(str(p))
    warnings = cfg.validation_warnings(config, p)
    assert len(warnings) == 1
    assert "not a valid regex" in warnings[0]
    assert "s(run" in warnings[0]


def test_no_warnings_when_script_present_and_regex_valid(tmp_path: Path) -> None:
    script = tmp_path / "x.sh"
    script.write_text("#!/bin/sh\n")
    p = _write(
        tmp_path,
        "clusters:\n  c:\n    host: h\n    budget:\n"
        "      script: x.sh\n      guard_commands: ['sbatch', 'aslurmx?']\n",
    )
    config = cfg.load_config(str(p))
    assert cfg.validation_warnings(config, p) == []


def test_no_warnings_when_no_budget(tmp_path: Path) -> None:
    p = _write(tmp_path, "clusters:\n  c:\n    host: h\n    user: u\n")
    config = cfg.load_config(str(p))
    assert cfg.validation_warnings(config, p) == []
