"""The command log: commands, the job IDs they submitted, `ctun logs` (ssh mocked)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cluster_tunnel import cmdlog, paths
from cluster_tunnel.cli import cli
from tests.test_cli import _cfg, _norm


@pytest.fixture(autouse=True)
def _cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")


def _log_file() -> Path:
    return paths.cmdlog_dir() / "localhost.jsonl"


def test_an_entry_carries_the_job_ids_it_submitted() -> None:
    entry_id = cmdlog.record("localhost", ["sbatch", "train.sh"])
    cmdlog.record_jobs("localhost", entry_id, ["4818213"])
    [entry] = cmdlog.entries("localhost")
    assert entry["command"] == ["sbatch", "train.sh"]
    assert entry["job_ids"] == ["4818213"]


def test_an_entry_without_jobs_has_no_job_ids() -> None:
    cmdlog.record("localhost", ["squeue", "--me"])
    [entry] = cmdlog.entries("localhost")
    assert "job_ids" not in entry


def test_job_ids_of_an_unknown_entry_are_ignored() -> None:
    cmdlog.record("localhost", ["sbatch", "a.sh"])
    cmdlog.record_jobs("localhost", "000000000000", ["1"])
    [entry] = cmdlog.entries("localhost")
    assert "job_ids" not in entry


def test_an_old_entry_without_id_stays_as_it_is() -> None:
    _log_file().parent.mkdir(parents=True)
    old = {"epoch": 1760000000, "command": ["sbatch", "old.sh"]}
    _log_file().write_text(json.dumps(old) + "\n", encoding="utf-8")
    cmdlog.record_jobs("localhost", "000000000000", ["1"])
    assert cmdlog.entries("localhost") == [old]


def test_summary_counts_only_commands() -> None:
    entry_id = cmdlog.record("localhost", ["sbatch", "a.sh"])
    cmdlog.record_jobs("localhost", entry_id, ["1", "2"])
    summary = cmdlog.summary("localhost")
    assert summary["count"] == 1
    assert summary["last_epoch"] == cmdlog.entries("localhost")[0]["epoch"]


def _patch_run(monkeypatch, output: str) -> None:
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: object())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: True)

    def fake_run(spec, tokens, tty=False, on_line=None):
        if on_line is not None:
            for line in output.splitlines():
                on_line(line)
        return 0

    monkeypatch.setattr("cluster_tunnel.ssh.run", fake_run)
    monkeypatch.setattr("cluster_tunnel.ssh.feed_script", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))


def test_run_logs_the_job_ids_it_submitted(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_run(monkeypatch, "Submitted batch job 4818213\n")
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "sbatch", "train.sh"])
    assert r.exit_code == 0, r.output
    [entry] = cmdlog.entries("localhost")
    assert entry["job_ids"] == ["4818213"]


def test_run_with_tty_logs_no_job_ids(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_run(monkeypatch, "Submitted batch job 4818213\n")
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--tty", "--", "sbatch", "train.sh"])
    assert r.exit_code == 0, r.output
    [entry] = cmdlog.entries("localhost")
    assert "job_ids" not in entry


def test_logs_shows_the_job_ids(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    entry_id = cmdlog.record("localhost", ["sbatch", "train.sh"])
    cmdlog.record_jobs("localhost", entry_id, ["4818213"])
    cmdlog.record("localhost", ["squeue", "--me"])

    text = CliRunner().invoke(cli, ["-t", "localhost", "logs"])
    assert "sbatch train.sh → job 4818213" in _norm(text)

    payload = json.loads(CliRunner().invoke(cli, ["-t", "localhost", "logs", "--json"]).output)
    assert [command.get("job_ids") for command in payload["commands"]] == [["4818213"], None]


def test_api_run_returns_the_job_ids_it_submitted(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel import api

    _cfg(tmp_path, monkeypatch)
    _patch_run(monkeypatch, "Submitted batch job 1\nSubmitted batch job 2\n")
    assert api.run("localhost", ["sbatch", "--array=0-1", "a.sh"]).job_ids == ["1", "2"]
    assert cmdlog.entries("localhost")[0]["job_ids"] == ["1", "2"]


def test_run_logs_a_job_id_before_the_command_ends(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel import api

    _cfg(tmp_path, monkeypatch)
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: object())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: True)

    def interrupted(spec, tokens, tty=False, on_line=None):
        on_line("Submitted batch job 5")
        raise KeyboardInterrupt  # Ctrl+C while `sbatch --wait` waits

    monkeypatch.setattr("cluster_tunnel.ssh.run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        api.run("localhost", ["sbatch", "--wait", "a.sh"])
    assert cmdlog.entries("localhost")[0]["job_ids"] == ["5"]
