"""CLI surface tests via Click's CliRunner (no real ssh)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cluster_tunnel.cli import cli


def _norm(result) -> str:
    """Collapse rich's line-wrapping so substring asserts are robust."""
    return " ".join(result.output.split())


def _cfg(tmp_path: Path, monkeypatch) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text("clusters:\n  localhost:\n    host: localhost\n")
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    return p


def test_help() -> None:
    r = CliRunner().invoke(cli, ["--help"])
    assert r.exit_code == 0
    assert "Usage" in _norm(r)


def test_version() -> None:
    r = CliRunner().invoke(cli, ["--version"])
    assert r.exit_code == 0
    assert "0.1.0" in r.output


def test_run_requires_command(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run"])
    assert r.exit_code != 0
    assert "Provide a command" in _norm(r)


def test_info_without_target_lists_all(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["info"])
    assert r.exit_code == 0
    assert "localhost" in _norm(r)


def test_unknown_cluster(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "nope", "info"])
    assert r.exit_code != 0
    assert "Unknown cluster" in _norm(r)


def test_webui_placeholder(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["webui"])
    assert r.exit_code == 0
    assert "not yet implemented" in _norm(r)
