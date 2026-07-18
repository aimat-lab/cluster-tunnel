"""CLI surface tests via Click's CliRunner (no real ssh)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cluster_tunnel.budget import Decision
from cluster_tunnel.cli import cli
from cluster_tunnel.constants import ExitCode


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
    from cluster_tunnel.constants import get_version

    r = CliRunner().invoke(cli, ["--version"])
    assert r.exit_code == 0
    # Assert against the packaged version so this never drifts on a bump.
    assert get_version() in r.output


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


def test_info_shows_password_required(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "clusters:\n"
        "  otponly:\n"
        "    host: h.example\n"
        "    requires_otp: true\n"
        "    requires_password: false\n"
    )
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    r = CliRunner().invoke(cli, ["-t", "otponly", "info"])
    assert r.exit_code == 0
    out = _norm(r)
    assert "Password required" in out


def test_info_json_includes_requires_password(tmp_path: Path, monkeypatch) -> None:
    import json

    _cfg(tmp_path, monkeypatch)  # localhost cluster, defaults -> requires_password true
    r = CliRunner().invoke(cli, ["-t", "localhost", "info", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    assert data["requires_password"] is True


def test_unknown_cluster(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "nope", "info"])
    assert r.exit_code != 0
    assert "Unknown cluster" in _norm(r)


def test_logout_without_target_clears_all(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "clusters:\n"
        "  alpha:\n    host: alpha.example\n"
        "  beta:\n    host: beta.example\n"
    )
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    r = CliRunner().invoke(cli, ["logout"])
    assert r.exit_code == 0
    out = _norm(r)
    assert "alpha" in out
    assert "beta" in out


def test_webui_placeholder(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["webui"])
    assert r.exit_code == 0
    assert "not yet implemented" in _norm(r)


def test_validate_ok(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)  # localhost cluster, no budget block
    r = CliRunner().invoke(cli, ["config", "--validate"])
    assert r.exit_code == 0
    assert "is valid" in _norm(r)


def test_validate_warns_on_missing_budget_script(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("clusters:\n  c:\n    host: h\n    budget:\n      script: budget/nope.sh\n")
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    r = CliRunner().invoke(cli, ["config", "--validate"])
    assert r.exit_code != 0
    out = _norm(r)
    assert "budget script not found" in out
    assert "warning(s)" in out


def test_validate_warns_on_bad_guard_regex(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "x.sh"
    script.write_text("#!/bin/sh\n")
    p = tmp_path / "config.yaml"
    p.write_text(
        "clusters:\n  c:\n    host: h\n    budget:\n"
        "      script: x.sh\n      guard_commands: ['s(run']\n"
    )
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    r = CliRunner().invoke(cli, ["config", "--validate"])
    assert r.exit_code != 0
    assert "not a valid regex" in _norm(r)


# --- shell completion ----------------------------------------------------------


def test_init_completion_emits_script() -> None:
    for shell in ("bash", "zsh", "fish"):
        r = CliRunner().invoke(cli, ["--init-completion", shell])
        assert r.exit_code == 0, shell
        assert "_CTUN_COMPLETE" in r.output


def test_init_completion_rejects_unknown_shell() -> None:
    r = CliRunner().invoke(cli, ["--init-completion", "tcsh"])
    assert r.exit_code != 0


def test_target_completion_lists_clusters(tmp_path: Path) -> None:
    from cluster_tunnel.cli import _complete_clusters

    p = tmp_path / "config.yaml"
    p.write_text("clusters:\n  alpha:\n    host: a\n  beta:\n    host: b\n")

    class _Ctx:
        params = {"config_path": str(p)}

    assert sorted(i.value for i in _complete_clusters(_Ctx(), None, "")) == ["alpha", "beta"]
    assert [i.value for i in _complete_clusters(_Ctx(), None, "al")] == ["alpha"]


def test_target_completion_handles_bad_config() -> None:
    from cluster_tunnel.cli import _complete_clusters

    class _Ctx:
        params = {"config_path": "/nonexistent/does-not-exist.yaml"}

    assert _complete_clusters(_Ctx(), None, "") == []


# --- upload / download ---------------------------------------------------------


def _patch_transfer(monkeypatch, *, live=True, rc=0):
    """Isolate transfer commands from ssh/rsync; capture the run_transfer call."""
    captured = {}
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: object())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: live)

    def fake_run_transfer(spec, direction, src, dest, *, dry_run=False, extra=()):
        captured.update(direction=direction, src=src, dest=dest,
                        dry_run=dry_run, extra=tuple(extra))
        return rc

    monkeypatch.setattr("cluster_tunnel.transfer.run_transfer", fake_run_transfer)
    return captured


def test_upload_dispatches(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    captured = _patch_transfer(monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "localhost", "upload", "./a", "data"])
    assert r.exit_code == 0
    assert captured == {"direction": "upload", "src": "./a", "dest": "data",
                        "dry_run": False, "extra": ()}


def test_download_dry_run_and_passthrough_and_exit_code(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    captured = _patch_transfer(monkeypatch, rc=23)
    r = CliRunner().invoke(
        cli, ["-t", "localhost", "download", "-n", "results", "out", "--", "--info=progress2"]
    )
    assert r.exit_code == 23  # rsync's exit code propagates
    assert captured["direction"] == "download"
    assert captured["src"] == "results" and captured["dest"] == "out"
    assert captured["dry_run"] is True
    assert captured["extra"] == ("--info=progress2",)


def test_upload_no_live_tunnel_exit_10(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_transfer(monkeypatch, live=False)
    r = CliRunner().invoke(cli, ["-t", "localhost", "upload", "./a", "data"])
    assert r.exit_code == ExitCode.LOGIN_REQUIRED
    assert "ctun-error: login_required" in r.output


# --- machine-readable JSON surface ---------------------------------------------


def test_info_json_includes_ctun_version(tmp_path: Path, monkeypatch) -> None:
    import json

    from cluster_tunnel.constants import get_version

    _cfg(tmp_path, monkeypatch)  # cluster 'localhost'
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: False)
    monkeypatch.setattr("cluster_tunnel.session.load", lambda name: None)
    r = CliRunner().invoke(cli, ["-t", "localhost", "info", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["ctun_version"] == get_version()
    assert payload["cluster"] == "localhost"


def test_status_json_always_includes_target_unit_limit(tmp_path: Path, monkeypatch) -> None:
    import json

    p = tmp_path / "config.yaml"
    p.write_text("clusters:\n  c:\n    host: h\n    user: u\n    budget:\n      unit: jobh\n")
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: False)
    monkeypatch.setattr("cluster_tunnel.session.load", lambda name: None)
    r = CliRunner().invoke(cli, ["status", "--json"])
    assert r.exit_code == 0
    rows = json.loads(r.output)
    assert len(rows) == 1
    row = rows[0]
    for key in ("cluster", "target", "live", "limit", "unit", "used"):
        assert key in row, key
    assert row["target"] == "u@h"
    assert row["unit"] == "jobh"   # no session -> falls back to configured budget unit
    assert row["limit"] is None    # no active session


# --- jobs overview -------------------------------------------------------------


def _patch_jobs(monkeypatch, *, live=True, jobs=None, error=None) -> None:
    """Isolate `jobs` from real ssh: stub conn_spec/is_live and jobs.query."""
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: object())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: live)

    def fake_query(spec, user, since_minutes):
        if error is not None:
            raise RuntimeError(error)
        return jobs or []

    monkeypatch.setattr("cluster_tunnel.jobs.query", fake_query)


def test_jobs_renders_table(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel.jobs import Job

    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch, jobs=[
        Job("active", "966737", "RUNNING", "14:44", "7:00:00", "booster", "1", "train.sh"),
    ])
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs"])
    assert r.exit_code == 0
    out = _norm(r)
    assert "966737" in out and "RUNNING" in out


def test_jobs_notes_down_tunnel(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch, live=False)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs"])
    assert r.exit_code == 0
    assert "tunnel down" in _norm(r)


def test_jobs_json_shape(tmp_path: Path, monkeypatch) -> None:
    import json

    from cluster_tunnel.jobs import Job

    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch, jobs=[
        Job("done", "960652", "CANCELLED by 30927", "01:56:26", "07:00:00", "booster", "1", "r.sh"),
    ])
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "--since", "2h", "--json"])
    assert r.exit_code == 0
    payload = json.loads(r.output)
    assert payload["since_minutes"] == 120
    assert payload["clusters"][0]["cluster"] == "localhost"
    job = payload["clusters"][0]["jobs"][0]
    assert job["jobid"] == "960652"
    assert job["state"] == "CANCELLED by 30927"  # full raw state kept in JSON


def test_jobs_query_error_is_surfaced_not_fatal(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch, error="squeue not found")
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs"])
    assert r.exit_code == 0
    assert "could not query jobs" in _norm(r)


def test_jobs_rejects_bad_since(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "--since", "bogus"])
    assert r.exit_code != 0
    assert "invalid duration" in _norm(r)


# --- run exit codes ------------------------------------------------------------
# `run` gives its own preflight failures stable exit codes + a `ctun-error:` marker,
# while still propagating the remote command's exit code unchanged.


def _patch_run(monkeypatch, *, live=True, decision=None, run_rc=0) -> None:
    """Isolate `run` from real ssh: stub conn_spec/is_live/run + budget.decide."""
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: object())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: live)
    monkeypatch.setattr("cluster_tunnel.ssh.run", lambda spec, tokens, tty=False: run_rc)
    monkeypatch.setattr("cluster_tunnel.cmdlog.record", lambda name, tokens: None)
    if decision is not None:
        monkeypatch.setattr("cluster_tunnel.budget.decide", lambda *a, **k: decision)


def test_run_no_live_tunnel_exit_10(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_run(monkeypatch, live=False)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "squeue"])
    assert r.exit_code == ExitCode.LOGIN_REQUIRED
    assert "ctun-error: login_required" in r.output


def test_run_budget_exhausted_exit_11(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    d = Decision(False, "session budget exhausted: 150 >= 100 jobh", 150.0, 100.0, "jobh")
    _patch_run(monkeypatch, decision=d)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "sbatch", "j"])
    assert r.exit_code == ExitCode.BUDGET_EXHAUSTED
    assert "ctun-error: budget_exhausted" in r.output


def test_run_budget_guard_error_exit_12(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    d = Decision(False, "budget probe failed: boom (fail-closed: blocked)", None, 100.0, "jobh")
    _patch_run(monkeypatch, decision=d)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "sbatch", "j"])
    assert r.exit_code == ExitCode.BUDGET_GUARD_ERROR
    assert "ctun-error: budget_guard_error" in r.output


def test_run_dry_run_blocked_exits_with_code(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    d = Decision(False, "session budget exhausted", 150.0, 100.0, "jobh")
    _patch_run(monkeypatch, decision=d)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "-n", "--", "sbatch", "j"])
    assert r.exit_code == ExitCode.BUDGET_EXHAUSTED
    assert "ctun-error: budget_exhausted" in r.output
    assert "BLOCK" in _norm(r)


def test_run_dry_run_allowed_exits_zero(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    d = Decision(True, "within budget", 5.0, 100.0, "jobh")
    _patch_run(monkeypatch, decision=d)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "-n", "--", "sbatch", "j"])
    assert r.exit_code == 0
    assert "ALLOW" in _norm(r)


def test_run_propagates_remote_exit_code(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    d = Decision(True, "not a guarded command")
    _patch_run(monkeypatch, decision=d, run_rc=3)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "false"])
    assert r.exit_code == 3


def test_run_warns_when_near_budget_limit(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    d = Decision(True, "within budget", 90.0, 100.0, "jobh")  # 90% used
    _patch_run(monkeypatch, decision=d, run_rc=0)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "sbatch", "j"])
    assert r.exit_code == 0
    out = _norm(r)
    assert "approaching limit" in out
    assert "90% used" in out


def test_run_no_warning_when_well_under_budget(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    d = Decision(True, "within budget", 10.0, 100.0, "jobh")  # 10% used
    _patch_run(monkeypatch, decision=d, run_rc=0)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "sbatch", "j"])
    assert r.exit_code == 0
    out = _norm(r)
    assert "approaching limit" not in out
    assert "10 / 100 jobh used" in out
