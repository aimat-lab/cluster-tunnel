"""The CLI's observable contract: what `run`, `login`, `status`, `logout` and `jobs` do.

Written against ctun 0.6.0 before its logic moved into :mod:`cluster_tunnel.api`,
so the move can't change exit codes, markers, messages, JSON or the session and
command-log side effects unnoticed. Everything runs on stubs; no real ssh.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cluster_tunnel import cmdlog, paths, popup, session, ssh, wsl
from cluster_tunnel.budget import Decision
from cluster_tunnel.cli import cli
from cluster_tunnel.constants import ExitCode

from tests.test_cli import _norm

LOCALHOST = "clusters:\n  localhost:\n    host: localhost\n"
GUARDED = (
    "clusters:\n"
    "  c:\n"
    "    host: h\n"
    "    user: u\n"
    "    budget:\n"
    "      session_limit: 3\n"
    "      unit: jobh\n"
)


@pytest.fixture(autouse=True)
def _cache(tmp_path: Path, monkeypatch) -> None:
    """Keep session files and command logs out of the real cache."""
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")


def _config(tmp_path: Path, monkeypatch, text: str = LOCALHOST) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    return p


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args))


# --- run ---------------------------------------------------------------------


def _stub_run(monkeypatch, *, live: bool = True, decision: Decision | None = None, rc: int = 0):
    """Stub the tunnel, the budget decision, the command log and ssh.run; record calls."""
    calls: dict[str, list] = {"decide": [], "record": [], "run": []}
    monkeypatch.setattr(ssh, "is_live", lambda spec: live)

    def fake_decide(config, name, spec, tokens, config_path):
        calls["decide"].append((name, list(tokens), Path(config_path)))
        return decision or Decision(True, "not a guarded command")

    def fake_run(spec, tokens, tty=False, on_line=None):
        calls["run"].append((list(tokens), tty))
        return rc

    monkeypatch.setattr("cluster_tunnel.budget.decide", fake_decide)
    monkeypatch.setattr(cmdlog, "record", lambda name, tokens: calls["record"].append((name, list(tokens))))
    monkeypatch.setattr(ssh, "run", fake_run)
    return calls


def test_run_checks_the_budget_logs_and_runs_the_command(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path, monkeypatch)
    calls = _stub_run(monkeypatch)
    r = _invoke("-t", "localhost", "run", "--", "squeue", "--me")
    assert r.exit_code == 0, r.output
    assert calls["decide"] == [("localhost", ["squeue", "--me"], cfg)]
    assert calls["record"] == [("localhost", ["squeue", "--me"])]
    assert calls["run"] == [(["squeue", "--me"], False)]
    assert "budget:" not in _norm(r)  # no probe ran, so no budget line


def test_run_passes_tty_to_ssh(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    calls = _stub_run(monkeypatch)
    r = _invoke("-t", "localhost", "run", "--tty", "--", "htop")
    assert r.exit_code == 0, r.output
    assert calls["run"] == [(["htop"], True)]


def test_run_leaves_options_after_the_command_to_the_command(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    calls = _stub_run(monkeypatch)
    r = _invoke("-t", "localhost", "run", "--note", "x", "python", "train.py", "--project", "p", "--tty", "-n")
    assert r.exit_code == 0, r.output
    assert calls["run"] == [(["python", "train.py", "--project", "p", "--tty", "-n"], False)]


def test_run_without_a_tunnel_names_both_logins(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    calls = _stub_run(monkeypatch, live=False)
    r = _invoke("-t", "localhost", "run", "--", "squeue")
    assert r.exit_code == ExitCode.LOGIN_REQUIRED
    out = _norm(r)
    assert "No live tunnel for 'localhost'" in out
    assert "ctun -t localhost login --interactive" in out
    assert calls == {"decide": [], "record": [], "run": []}


def test_an_exhausted_budget_blocks_with_the_numbers(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    d = Decision(False, "session budget exhausted: 150.0 >= 100.0 jobh", 150.0, 100.0, "jobh")
    calls = _stub_run(monkeypatch, decision=d)
    r = _invoke("-t", "localhost", "run", "--", "sbatch", "j")
    assert r.exit_code == ExitCode.BUDGET_EXHAUSTED
    out = _norm(r)
    assert "ctun-error: budget_exhausted" in out
    assert "Submission blocked on 'localhost': compute budget exhausted." in out
    assert "used 150 / 100 jobh (over by 50 jobh)" in out
    assert "'sbatch j' was NOT submitted." in out
    assert calls["record"] == [] and calls["run"] == []


def test_an_unverifiable_budget_blocks_with_the_reason(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    d = Decision(False, "budget probe failed: boom (fail-closed: blocked)", None, 100.0, "jobh")
    calls = _stub_run(monkeypatch, decision=d)
    r = _invoke("-t", "localhost", "run", "--", "sbatch", "j")
    assert r.exit_code == ExitCode.BUDGET_GUARD_ERROR
    out = _norm(r)
    assert "ctun-error: budget_guard_error" in out
    assert (
        "Submission blocked on 'localhost': budget probe failed: boom (fail-closed: blocked). "
        "Command not submitted." in out
    )
    assert calls["record"] == [] and calls["run"] == []


def test_dry_run_prints_the_decision_and_runs_nothing(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    d = Decision(True, "within budget: 5.0 < 100.0 jobh", 5.0, 100.0, "jobh")
    calls = _stub_run(monkeypatch, decision=d)
    r = _invoke("-t", "localhost", "run", "-n", "--", "sbatch", "j")
    assert r.exit_code == 0
    out = _norm(r)
    assert "[dry-run] ALLOW on localhost: sbatch j" in out
    assert "budget: 5.0 / 100.0 jobh" in out
    assert "reason: within budget: 5.0 < 100.0 jobh" in out
    assert calls["record"] == [] and calls["run"] == []


def test_dry_run_of_an_unverifiable_budget_exits_12(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    d = Decision(False, "no active session (fail-closed: blocked)", None, None, "jobh")
    _stub_run(monkeypatch, decision=d)
    r = _invoke("-t", "localhost", "run", "-n", "--", "sbatch", "j")
    assert r.exit_code == ExitCode.BUDGET_GUARD_ERROR
    out = _norm(r)
    assert "ctun-error: budget_guard_error" in out
    assert "[dry-run] BLOCK on localhost: sbatch j" in out
    assert "budget:" not in out  # no usage number, so no budget line


# --- login -------------------------------------------------------------------


def _tunnel(monkeypatch, *states: bool) -> None:
    """``ssh.is_live`` answers ``states`` in order, then keeps the last one."""
    answers = iter(states)
    last = states[-1]
    monkeypatch.setattr(ssh, "is_live", lambda spec: next(answers, last))


def _no_master(*args, **kwargs):
    raise AssertionError("must not open a master")


def test_login_on_a_live_tunnel_keeps_the_session(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    session.start("localhost", limit=5.0, unit="units")
    cmdlog.record("localhost", ["ls"])
    before = session.load("localhost")
    _tunnel(monkeypatch, True)
    monkeypatch.setattr(ssh, "open_master", _no_master)
    r = _invoke("-t", "localhost", "login")
    assert r.exit_code == 0, r.output
    out = _norm(r)
    assert "already live" in out
    assert "existing session kept; re-auth would reset the budget window" in out
    assert "budget 5.0 units" in out
    assert session.load("localhost") == before
    assert cmdlog.summary("localhost")["count"] == 1


def test_login_on_a_live_tunnel_without_a_session_starts_one(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, GUARDED)
    _tunnel(monkeypatch, True)
    monkeypatch.setattr(ssh, "open_master", _no_master)
    r = _invoke("-t", "c", "login")
    assert r.exit_code == 0, r.output
    assert "existing session kept" not in _norm(r)
    sess = session.load("c")
    assert sess["limit"] == 3.0 and sess["unit"] == "jobh"


@pytest.mark.parametrize(
    "args, limit, shown",
    [
        ([], 3.0, "budget 3.0 jobh"),
        (["--limit", "7"], 7.0, "budget 7.0 jobh"),
        (["--limit", "-1"], None, "budget unguarded (no limit set)"),
    ],
)
def test_login_session_limit_precedence(tmp_path: Path, monkeypatch, args, limit, shown) -> None:
    _config(tmp_path, monkeypatch, GUARDED)
    _tunnel(monkeypatch, False, True)
    monkeypatch.setattr(ssh, "open_master", lambda spec, verbose=0: 0)
    r = _invoke("-t", "c", "login", *args)
    assert r.exit_code == 0, r.output
    out = _norm(r)
    assert "Tunnel to c established." in out
    assert "target u@h" in out
    assert shown in out
    assert session.load("c")["limit"] == limit


@pytest.mark.parametrize("rc, live_after", [(255, False), (0, False)])
def test_login_failure_starts_no_session(tmp_path: Path, monkeypatch, rc, live_after) -> None:
    _config(tmp_path, monkeypatch)
    _tunnel(monkeypatch, False, live_after)
    monkeypatch.setattr(ssh, "open_master", lambda spec, verbose=0: rc)
    r = _invoke("-t", "localhost", "login")
    assert r.exit_code == 1
    out = _norm(r)
    assert f"Failed to open tunnel to 'localhost' (ssh exit {rc})." in out
    assert "Re-run with -v (or -vv/-vvv) for ssh diagnostics." in out
    assert session.load("localhost") is None


def test_verbose_login_failure_prints_the_ssh_diagnostics(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    _tunnel(monkeypatch, False)
    monkeypatch.setattr(ssh, "open_master", lambda spec, verbose=0: 255)
    monkeypatch.setattr(
        ssh, "check",
        lambda spec: subprocess.CompletedProcess([], 255, "", "Control socket connect: refused\n"),
    )
    r = _invoke("-t", "localhost", "login", "-v")
    assert r.exit_code == 1
    out = _norm(r)
    assert "Control socket connect: refused" in out
    assert "Re-run with -v" not in out


def test_login_reports_a_wsl_error(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    _tunnel(monkeypatch, False)

    def broken(spec, verbose=0):
        raise wsl.WslError("cannot prepare the control socket in WSL")

    monkeypatch.setattr(ssh, "open_master", broken)
    r = _invoke("-t", "localhost", "login")
    assert r.exit_code == 1
    assert "cannot prepare the control socket in WSL" in _norm(r)


def test_interactive_login_needs_a_display(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    _tunnel(monkeypatch, False)
    monkeypatch.setattr(popup, "gui_available", lambda: False)
    r = _invoke("-t", "localhost", "login", "-i")
    assert r.exit_code == 1
    assert "No display for the login dialog; run `ctun -t localhost login` from a terminal." in _norm(r)


def test_interactive_login_cancelled(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    _tunnel(monkeypatch, False)
    monkeypatch.setattr(popup, "gui_available", lambda: True)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a, **k: None)
    r = _invoke("-t", "localhost", "login", "-i")
    assert r.exit_code == 1
    assert "Login cancelled." in _norm(r)
    assert session.load("localhost") is None


def test_interactive_login_that_fails(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    _tunnel(monkeypatch, False)
    monkeypatch.setattr(popup, "gui_available", lambda: True)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a, **k: popup.Credentials("pw", None, None))
    monkeypatch.setattr(popup, "open_with_password", lambda *a, **k: (False, ""))
    r = _invoke("-t", "localhost", "login", "-i")
    assert r.exit_code == 1
    assert "Interactive login to 'localhost' failed or timed out." in _norm(r)
    assert session.load("localhost") is None


def test_interactive_login_that_fails_shows_why(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    _tunnel(monkeypatch, False)
    monkeypatch.setattr(popup, "gui_available", lambda: True)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a, **k: popup.Credentials("pw", None, None))
    monkeypatch.setattr(popup, "open_with_password",
                        lambda *a, **k: (False, "Host key verification failed.\n"))
    r = _invoke("-t", "localhost", "login", "-i")
    assert r.exit_code == 1
    assert "Host key verification failed." in _norm(r)


def test_interactive_login_reports_a_wsl_error(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch)
    _tunnel(monkeypatch, False)
    monkeypatch.setattr(popup, "gui_available", lambda: True)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a, **k: popup.Credentials("pw", None, None))

    def broken(*args, **kwargs):
        raise wsl.WslError("WSL did not respond")

    monkeypatch.setattr(popup, "open_with_password", broken)
    r = _invoke("-t", "localhost", "login", "-i")
    assert r.exit_code == 1
    assert "WSL did not respond" in _norm(r)


def test_interactive_login_uses_the_dialog_and_its_limit(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, GUARDED)
    _tunnel(monkeypatch, False)
    monkeypatch.setattr(popup, "gui_available", lambda: True)
    asked: list[tuple] = []
    logins: list[tuple] = []

    def fake_prompt(*args):
        asked.append(args)
        return popup.Credentials("pw", "123456", 9.0)

    def fake_login(spec, password, otp, timeout, verbose):
        logins.append((spec.name, password, otp, timeout, verbose))
        return True, ""

    monkeypatch.setattr(popup, "prompt_credentials", fake_prompt)
    monkeypatch.setattr(popup, "open_with_password", fake_login)
    r = _invoke("-t", "c", "login", "-i", "--timeout", "30")
    assert r.exit_code == 0, r.output
    assert "established (interactive)" in _norm(r)
    assert asked == [("c", "u@h", 3.0, "jobh", False, True)]
    assert logins == [("c", "pw", "123456", 30, 0)]
    assert session.load("c")["limit"] == 9.0


# --- status ------------------------------------------------------------------


def _status_rows(*args: str) -> list[dict]:
    r = _invoke(*args, "status", "--json")
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def _probe(monkeypatch, used: float | Exception) -> list[tuple]:
    """Stub the remote budget probe; return the list of probes made."""
    probes: list[tuple] = []
    monkeypatch.setattr("cluster_tunnel.budget.remote_user", lambda spec, cluster: "u")

    def fake_used(spec, script, start_epoch, user):
        probes.append((Path(script).name, start_epoch, user))
        if isinstance(used, Exception):
            raise used
        return used

    monkeypatch.setattr("cluster_tunnel.budget.used_since", fake_used)
    return probes


def test_status_json_of_a_targeted_live_guarded_cluster(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, GUARDED)
    sess = session.start("c", limit=10.0, unit="jobh")
    cmdlog.record("c", ["sbatch", "a.sh"])
    _tunnel(monkeypatch, True)
    probes = _probe(monkeypatch, 4.5)
    [row] = _status_rows("-t", "c")
    assert row == {
        "cluster": "c",
        "target": "u@h",
        "live": True,
        "session": sess,
        "limit": 10.0,
        "unit": "jobh",
        "commands": cmdlog.summary("c"),
        "used": 4.5,
        "used_error": None,
    }
    assert probes == [("c.sh", sess["start_epoch"], "u")]


@pytest.mark.parametrize(
    "targeted, live, limit, text",
    [
        (False, True, 10.0, GUARDED),  # the all-clusters overview never probes
        (True, False, 10.0, GUARDED),  # no tunnel
        (True, True, None, GUARDED),  # unguarded session
        (True, True, 10.0, "clusters:\n  c:\n    host: h\n"),  # no budget configured
    ],
)
def test_status_probes_usage_only_when_it_can_matter(
    tmp_path: Path, monkeypatch, targeted, live, limit, text
) -> None:
    _config(tmp_path, monkeypatch, text)
    session.start("c", limit=limit, unit="jobh")
    _tunnel(monkeypatch, live)
    probes = _probe(monkeypatch, 1.0)
    [row] = _status_rows(*(["-t", "c"] if targeted else []))
    assert probes == []
    assert row["used"] is None and row["used_error"] is None


def test_status_reports_a_failed_probe(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, GUARDED)
    session.start("c", limit=10.0, unit="jobh")
    _tunnel(monkeypatch, True)
    _probe(monkeypatch, RuntimeError("probe exited 1"))
    [row] = _status_rows("-t", "c")
    assert row["used"] is None and row["used_error"] == "probe exited 1"
    monkeypatch.setenv("COLUMNS", "200")
    assert "unavailable" in _norm(_invoke("-t", "c", "status"))


def test_status_without_a_session_falls_back_to_the_configured_unit(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, GUARDED)
    _tunnel(monkeypatch, False)
    [row] = _status_rows()
    assert row["session"] is None and row["limit"] is None and row["unit"] == "jobh"
    assert row["commands"] == {"count": 0, "last_epoch": None}


def test_status_table(tmp_path: Path, monkeypatch) -> None:
    _config(
        tmp_path, monkeypatch,
        "clusters:\n  a:\n    host: a.example\n  b:\n    host: b.example\n"
        "  d:\n    host: d.example\n  e:\n    host: e.example\n",
    )
    session.start("a", limit=5.0, unit="units")
    session.start("b", limit=None, unit="units")
    session.start("d", limit=-1, unit="units")
    monkeypatch.setattr(ssh, "is_live", lambda spec: spec.name == "a")
    monkeypatch.setenv("COLUMNS", "200")
    r = _invoke("status")
    assert r.exit_code == 0, r.output
    out = _norm(r)
    for word in ("live", "down", "limit 5.0 units", "unguarded", "unlimited"):
        assert word in out, word


def test_status_and_logout_with_no_clusters(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, "clusters: {}\n")
    for command in ("status", "logout"):
        r = _invoke(command)
        assert r.exit_code == 0
        assert "No clusters configured. Run `ctun config --init`." in _norm(r)


# --- logout ------------------------------------------------------------------


@pytest.mark.parametrize(
    "live, message",
    [
        (True, "Tunnel to 'localhost' closed; session cleared."),
        (False, "No live tunnel for 'localhost'; session cleared."),
    ],
)
def test_logout_closes_the_master_and_clears_the_state(tmp_path: Path, monkeypatch, live, message) -> None:
    _config(tmp_path, monkeypatch)
    session.start("localhost", limit=5.0, unit="units")
    cmdlog.record("localhost", ["ls"])
    closed: list[str] = []
    _tunnel(monkeypatch, live)
    monkeypatch.setattr(ssh, "close", lambda spec: closed.append(spec.name) or live)
    r = _invoke("-t", "localhost", "logout")
    assert r.exit_code == 0
    assert message in _norm(r)
    assert closed == ["localhost"]
    assert session.load("localhost") is None
    assert cmdlog.summary("localhost")["count"] == 0


# --- jobs --------------------------------------------------------------------


def test_jobs_sweep_queries_only_live_clusters(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel.jobs import Job, Overview

    _config(
        tmp_path, monkeypatch,
        "clusters:\n  alpha:\n    host: a.example\n    user: ua\n  beta:\n    host: b.example\n",
    )
    monkeypatch.setattr(ssh, "is_live", lambda spec: spec.name == "alpha")
    queries: list[tuple] = []

    def fake_query(spec, user, since_minutes):
        queries.append((spec.name, user, since_minutes))
        return Overview("haic", [Job("active", "1", "RUNNING", "0:01", "1:00", "cpu", "1", "N/A", "a.sh")])

    monkeypatch.setattr("cluster_tunnel.jobs.query", fake_query)
    r = _invoke("jobs", "--since", "0", "--json")
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["since_minutes"] == 0
    assert [(c["cluster"], c["live"], len(c["jobs"]), c["error"]) for c in payload["clusters"]] == [
        ("alpha", True, 1, None),
        ("beta", False, 0, None),
    ]
    assert queries == [("alpha", "ua", 0)]


def test_jobs_with_no_clusters(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, "clusters: {}\n")
    r = _invoke("jobs")
    assert r.exit_code == 0
    assert "No clusters configured. Run `ctun config --init`." in _norm(r)


# --- guard_scope through the CLI (added with the opt-in scope) -------------------


def test_every_command_scope_blocks_a_submission_inside_bash_c(tmp_path: Path, monkeypatch) -> None:
    _config(tmp_path, monkeypatch, GUARDED + "      guard_scope: every-command\n")
    (tmp_path / "budget").mkdir()
    (tmp_path / "budget" / "c.sh").write_text("echo 9\n", encoding="utf-8")
    session.start("c", limit=3.0, unit="jobh")
    monkeypatch.setattr(ssh, "is_live", lambda spec: True)
    monkeypatch.setattr(ssh, "probe", lambda *a: subprocess.CompletedProcess([], 0, "9\n", ""))
    monkeypatch.setattr(ssh, "run", lambda *a, **k: pytest.fail("must not run"))
    r = _invoke("-t", "c", "run", "--", "bash", "-c", "cd x && sbatch j.sh")
    assert r.exit_code == ExitCode.BUDGET_EXHAUSTED
    assert "ctun-error: budget_exhausted" in _norm(r)

    info = json.loads(_invoke("-t", "c", "info", "--json").output)
    assert info["budget"]["guard_scope"] == "every-command"


@pytest.mark.parametrize("command", ["status", "logout", "jobs"])
def test_an_empty_target_means_every_cluster(tmp_path: Path, monkeypatch, command: str) -> None:
    # `ctun -t "$UNSET" status` acted on every cluster in 0.6.0, as `if self.target:` saw no target.
    _config(tmp_path, monkeypatch)
    _tunnel(monkeypatch, False)
    r = _invoke("-t", "", command)
    assert r.exit_code == 0, r.output
    assert "localhost" in _norm(r)
