"""The API and the CLI agree: each scenario runs through both, on the same stubs.

``ctun`` is built on :mod:`cluster_tunnel.api`, and these tests hold it there:
for every scenario the exit code ``ctun`` ends with is the API's (the raised
error's ``exit_code``, or the result's), and both surfaces make the same calls
and leave the same sessions and command logs behind.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, Optional

import pytest
from click.testing import CliRunner

from cluster_tunnel import api, cmdlog, login_bridge, paths, popup, records, session, ssh
from cluster_tunnel.budget import Decision
from cluster_tunnel.cli import cli
from cluster_tunnel.jobs import Job, Overview

from tests.test_cli import _norm
from tests.test_records import FakeCluster

CONFIG = (
    "clusters:\n"
    "  c:\n"
    "    host: h\n"
    "    user: u\n"
    "    budget:\n"
    "      session_limit: 3\n"
    "      unit: jobh\n"
    "  d:\n"
    "    host: d.example\n"
)


@pytest.fixture(autouse=True)
def _config(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("CTUN_CONFIG", str(p))
    _use_cache(tmp_path, monkeypatch, "cli")


def _use_cache(tmp_path: Path, monkeypatch, surface: str) -> None:
    """Give a surface its own cache dir, so each starts from the same state."""
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / surface)


def _cli(*args: str):
    return CliRunner().invoke(cli, list(args))


def _outcome(call: Callable) -> tuple[int, Optional[str], object]:
    """The exit code ``ctun`` would end with for an API call, its marker, and the value."""
    try:
        value = call()
    except api.ClusterTunnelError as exc:
        return (1 if exc.exit_code is None else int(exc.exit_code)), exc.marker, exc
    code = value.exit_code if isinstance(value, api.Result) else 0
    return code, None, value


# --- run and run -n --------------------------------------------------------------

EXHAUSTED = Decision(False, "session budget exhausted: 150.0 >= 100.0 jobh", 150.0, 100.0, "jobh")
UNVERIFIABLE = Decision(False, "budget probe failed: boom (fail-closed: blocked)", None, 100.0, "jobh")
RUN_CASES = {
    "no tunnel": (False, None, 0),
    "unguarded": (True, Decision(True, "not a guarded command"), 0),
    "remote exit code": (True, Decision(True, "not a guarded command"), 3),
    "within budget": (True, Decision(True, "within budget: 10.0 < 100.0 jobh", 10.0, 100.0, "jobh"), 0),
    "near the limit": (True, Decision(True, "within budget: 90.0 < 100.0 jobh", 90.0, 100.0, "jobh"), 0),
    "exhausted": (True, EXHAUSTED, 0),
    "unverifiable": (True, UNVERIFIABLE, 0),
}


def _stub_run(monkeypatch, live: bool, decision: Optional[Decision], rc: int) -> dict:
    calls: dict = {"decide": [], "record": [], "run": []}
    monkeypatch.setattr(ssh, "is_live", lambda spec: live)

    def fake_decide(config, name, spec, tokens, config_path):
        calls["decide"].append((name, list(tokens), Path(config_path)))
        return decision

    monkeypatch.setattr("cluster_tunnel.budget.decide", fake_decide)
    monkeypatch.setattr(cmdlog, "record", lambda name, tokens: calls["record"].append((name, list(tokens))))
    monkeypatch.setattr(ssh, "run", lambda spec, tokens, tty=False, on_line=None: calls["run"].append((list(tokens), tty)) or rc)
    return calls


@pytest.mark.parametrize("case", RUN_CASES)
@pytest.mark.parametrize("tty", [False, True])
def test_run(monkeypatch, case: str, tty: bool) -> None:
    live, decision, rc = RUN_CASES[case]
    cli_calls = _stub_run(monkeypatch, live, decision, rc)
    r = _cli("-t", "c", "run", *(["--tty"] if tty else []), "--", "sbatch", "j.sh")

    api_calls = _stub_run(monkeypatch, live, decision, rc)
    code, marker, _ = _outcome(lambda: api.run("c", ["sbatch", "j.sh"], tty=tty))

    assert r.exit_code == code
    assert cli_calls == api_calls
    if marker:
        assert f"ctun-error: {marker}" in r.output


@pytest.mark.parametrize("case", RUN_CASES)
def test_dry_run_is_preflight(monkeypatch, case: str) -> None:
    live, decision, rc = RUN_CASES[case]
    cli_calls = _stub_run(monkeypatch, live, decision, rc)
    r = _cli("-t", "c", "run", "-n", "--", "sbatch", "j.sh")

    api_calls = _stub_run(monkeypatch, live, decision, rc)
    code, marker, value = _outcome(lambda: api.preflight("c", ["sbatch", "j.sh"]))

    assert r.exit_code == code
    assert cli_calls == api_calls and api_calls["run"] == []
    if marker:
        assert f"ctun-error: {marker}" in r.output
    shown = value.decision if isinstance(value, api.BudgetBlocked) else value
    if isinstance(shown, Decision):
        assert f"reason: {shown.reason}" in _norm(r)


# --- status and jobs ---------------------------------------------------------------


@pytest.mark.parametrize("probe", [2.0, RuntimeError("probe exited 1")])
@pytest.mark.parametrize("targeted", [False, True])
def test_status(monkeypatch, probe, targeted: bool) -> None:
    session.start("c", limit=3.0, unit="jobh")
    cmdlog.record("c", ["sbatch", "a.sh"])
    monkeypatch.setattr(ssh, "is_live", lambda spec: spec.name == "c")
    monkeypatch.setattr("cluster_tunnel.budget.remote_user", lambda spec, cluster: "u")

    def used_since(*args):
        if isinstance(probe, Exception):
            raise probe
        return probe

    monkeypatch.setattr("cluster_tunnel.budget.used_since", used_since)
    args = ["-t", "c"] if targeted else []
    r = _cli(*args, "status", "--json")
    assert r.exit_code == 0, r.output
    rows = [row.as_dict() for row in api.status("c" if targeted else None, usage=targeted)]
    assert json.loads(r.output) == rows


@pytest.mark.parametrize("since, minutes", [("24h", 1440), ("0", 0), ("90m", 90)])
@pytest.mark.parametrize("error", [None, "squeue not found"])
def test_jobs(monkeypatch, since: str, minutes: int, error: Optional[str]) -> None:
    monkeypatch.setattr(ssh, "is_live", lambda spec: spec.name == "c")
    queries: list = []

    def fake_query(spec, user, since_minutes):
        queries.append((spec.name, user, since_minutes))
        if error:
            raise RuntimeError(error)
        return Overview("haic", [Job("active", "7", "PENDING", "0:00", "2:00:00", "cpu", "1", "N/A", "j.sh")])

    monkeypatch.setattr("cluster_tunnel.jobs.query", fake_query)
    monkeypatch.setattr("cluster_tunnel.records.list_recent", lambda spec, since, ids: [])
    r = _cli("jobs", "--since", since, "--json")
    assert r.exit_code == 0, r.output
    entries = [entry.as_dict() for entry in api.jobs(since_minutes=minutes, with_records=True)]
    assert json.loads(r.output) == {"since_minutes": minutes, "clusters": entries}
    assert queries[: len(queries) // 2] == queries[len(queries) // 2:] == [("c", "u", minutes)]


# --- job records and projects ------------------------------------------------------


def _store(monkeypatch, fail: Optional[str] = None) -> FakeCluster:
    """A fresh record store on the cluster; ``fail`` makes storing fail with that message."""
    cluster = FakeCluster(known_refs=("job:9",))
    cluster.written = {"7": {"job_id": "7", "project": "htfm"}, "8": {"job_id": "8", "project": "htfm/mh"}}
    monkeypatch.setattr(ssh, "feed_script", cluster.feed_script)
    if fail:

        def failing(spec, pairs):
            raise records.UpdateError(fail, [])

        monkeypatch.setattr(records, "update_many", failing)
    return cluster


def _projects(cluster: FakeCluster) -> dict:
    return {job_id: record.get("project") for job_id, record in cluster.written.items()}


@pytest.mark.parametrize("targeted", [False, True])
def test_projects(monkeypatch, targeted: bool) -> None:
    monkeypatch.setattr(ssh, "is_live", lambda spec: spec.name == "c")
    _store(monkeypatch)
    r = _cli(*(["-t", "c"] if targeted else []), "projects", "list", "--json")
    assert r.exit_code == 0, r.output
    entries = [entry.as_dict() for entry in api.projects("c" if targeted else None)]
    assert json.loads(r.output) == {"clusters": entries}


RENAME_CASES = {
    # live, storing fails with, exit code with a target, without one
    "renamed": (True, None, 0, 0),
    "tunnel down": (False, None, 10, 0),
    "storing fails": (True, "could not store the records of jobs 7, 8: boom", 1, 1),
}


@pytest.mark.parametrize("case", RENAME_CASES)
@pytest.mark.parametrize("targeted", [False, True])
def test_projects_rename(monkeypatch, case: str, targeted: bool) -> None:
    live, fail, targeted_code, sweep_code = RENAME_CASES[case]
    monkeypatch.setattr(ssh, "is_live", lambda spec: live and spec.name == "c")
    via_cli = _store(monkeypatch, fail)
    r = _cli(*(["-t", "c"] if targeted else []), "projects", "rename", "htfm", "eval", "--json")
    via_api = _store(monkeypatch, fail)
    entries = [entry.as_dict() for entry in api.rename_project("htfm", "eval", "c" if targeted else None)]
    assert r.exit_code == (targeted_code if targeted else sweep_code), r.output
    if r.exit_code != 10:
        assert json.loads(r.stdout) == {"clusters": entries}
    assert _projects(via_cli) == _projects(via_api)


EDIT_CASES = {
    "edited": (True, ["7", "8", "9"]),
    "unknown job": (True, ["7", "999"]),
    "no job id": (True, ["7", "../x"]),
    "tunnel down": (False, ["7"]),
}


@pytest.mark.parametrize("case", EDIT_CASES)
def test_jobs_edit(monkeypatch, case: str) -> None:
    live, job_ids = EDIT_CASES[case]
    monkeypatch.setattr(ssh, "is_live", lambda spec: live)
    via_cli = _store(monkeypatch)
    r = _cli("-t", "c", "jobs", "edit", *job_ids, "--project", "eval")
    via_api = _store(monkeypatch)
    code, marker, value = _outcome(lambda: api.edit_jobs("c", job_ids, project="eval"))
    assert r.exit_code == code, r.output
    if marker:
        assert f"ctun-error: {marker}" in _norm(r)
    if code == 0:  # the same records, but for when each edit was made

        def undated(record: dict) -> dict:
            return {**record, "changes": [{**change, "at": None} for change in record["changes"]]}

        assert [undated(record) for record in json.loads(r.stdout)] == [undated(edit.record) for edit in value]
    assert _projects(via_cli) == _projects(via_api)


# --- login and logout --------------------------------------------------------------


def _master(monkeypatch, *, before: bool, after: bool, rc: int = 0) -> None:
    answers = iter([before, after])
    monkeypatch.setattr(ssh, "is_live", lambda spec: next(answers, after))
    monkeypatch.setattr(ssh, "open_master", lambda spec, verbose=0: rc)


def _dialog(monkeypatch, creds: Optional[popup.Credentials], ok: bool = True) -> None:
    monkeypatch.setattr(popup, "gui_available", lambda: True)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a: creds)
    monkeypatch.setattr(popup, "open_with_password", lambda *a: (ok, ""))


LOGIN_CASES = {
    "fresh": ([], {}, lambda mp: _master(mp, before=False, after=True), False),
    "fresh with a limit": (["--limit", "7"], {"limit": 7}, lambda mp: _master(mp, before=False, after=True), False),
    "fresh and unguarded": (["--limit", "-1"], {"limit": -1}, lambda mp: _master(mp, before=False, after=True), False),
    "live with a session": ([], {}, lambda mp: _master(mp, before=True, after=True), True),
    "live without a session": ([], {}, lambda mp: _master(mp, before=True, after=True), False),
    "ssh fails": ([], {}, lambda mp: _master(mp, before=False, after=False, rc=255), False),
    "master dies": ([], {}, lambda mp: _master(mp, before=False, after=False), False),
    "dialog": (["-i"], {"interactive": True},
               lambda mp: (_master(mp, before=False, after=True), _dialog(mp, popup.Credentials("pw", "1", 9.0))), False),
    "dialog cancelled": (["-i"], {"interactive": True},
                         lambda mp: (_master(mp, before=False, after=True), _dialog(mp, None)), False),
    "dialog login fails": (["-i"], {"interactive": True},
                           lambda mp: (_master(mp, before=False, after=False),
                                       _dialog(mp, popup.Credentials("pw", None, None), ok=False)), False),
    "bridge": (["-i"], {"interactive": True},
               lambda mp: (_master(mp, before=False, after=True), _bridge(mp, login_bridge.Outcome("live"))), False),
    "bridge says live, but isn't": (["-i"], {"interactive": True},
                                    lambda mp: (_master(mp, before=False, after=False),
                                                _bridge(mp, login_bridge.Outcome("live"))), False),
    "bridge cancelled": (["-i"], {"interactive": True},
                         lambda mp: (_master(mp, before=False, after=True),
                                     _bridge(mp, login_bridge.Outcome("cancelled"))), False),
    "bridge declines": (["-i"], {"interactive": True},
                        lambda mp: (_master(mp, before=False, after=True), _bridge(mp, login_bridge.Unavailable()),
                                    _dialog(mp, popup.Credentials("pw", "1", 9.0))), False),
}


def _bridge(monkeypatch, outcome) -> None:
    """A front end behind the login bridge that answers ``outcome`` (or raises it)."""

    def log_in(name, limit, *, config, timeout):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(login_bridge, "log_in", log_in)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a: pytest.fail("the popup must stay shut"))


def _state() -> tuple:
    sess = session.load("c")
    kept = sess and (sess.get("limit"), sess.get("unit"))
    return kept, cmdlog.summary("c")["count"]


@pytest.mark.parametrize("case", LOGIN_CASES)
def test_login(tmp_path: Path, monkeypatch, case: str) -> None:
    args, kwargs, stub, with_session = LOGIN_CASES[case]

    def prepare(surface: str) -> None:
        _use_cache(tmp_path, monkeypatch, surface)
        if with_session:
            session.start("c", limit=5.0, unit="units")
            cmdlog.record("c", ["ls"])
        stub(monkeypatch)

    prepare("cli")
    r = _cli("-t", "c", "login", *args)
    cli_state = _state()

    prepare("api")
    code, _, value = _outcome(lambda: api.login("c", **kwargs))
    api_state = _state()

    assert r.exit_code == code
    assert cli_state == api_state
    if isinstance(value, api.LoginError):
        assert str(value) in _norm(r)
    else:
        assert ("existing session kept" in _norm(r)) == value.session_kept


@pytest.mark.parametrize("target", [None, "c"])
def test_logout(tmp_path: Path, monkeypatch, target: Optional[str]) -> None:
    def prepare(surface: str) -> list:
        _use_cache(tmp_path, monkeypatch, surface)
        session.start("c", limit=5.0, unit="units")
        session.start("d", limit=None, unit="units")
        cmdlog.record("c", ["ls"])
        closed: list = []
        monkeypatch.setattr(ssh, "is_live", lambda spec: spec.name == "c")
        monkeypatch.setattr(ssh, "close", lambda spec: closed.append(spec.name) or True)
        return closed

    cli_closed = prepare("cli")
    r = _cli(*(["-t", target] if target else []), "logout")
    cli_state = (session.load("c"), session.load("d"), cmdlog.summary("c"))

    api_closed = prepare("api")
    results = api.logout(target)
    api_state = (session.load("c"), session.load("d"), cmdlog.summary("c"))

    assert r.exit_code == 0
    assert cli_closed == api_closed
    assert cli_state == api_state
    for item in results:
        word = "closed" if item.was_live else "No live tunnel"
        assert word in _norm(r) and f"'{item.cluster}'" in _norm(r)


# --- upload and download -----------------------------------------------------------


@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("live, rc", [(True, 0), (True, 23), (False, 0)])
def test_transfers(monkeypatch, direction: str, live: bool, rc: int) -> None:
    def stub() -> list:
        calls: list = []
        monkeypatch.setattr(ssh, "is_live", lambda spec: live)

        def fake_run_transfer(spec, dir_, src, dest, *, dry_run=False, extra=()):
            calls.append((spec.name, dir_, src, dest, dry_run, tuple(extra)))
            return rc

        monkeypatch.setattr("cluster_tunnel.transfer.run_transfer", fake_run_transfer)
        return calls

    cli_calls = stub()
    r = _cli("-t", "c", direction, "-n", "a", "b", "--", "-z")
    api_calls = stub()
    call = api.upload if direction == "upload" else api.download
    code, marker, _ = _outcome(lambda: call("c", "a", "b", dry_run=True, extra=("-z",)))

    assert r.exit_code == code
    assert cli_calls == api_calls
    if marker:
        assert f"ctun-error: {marker}" in r.output


def test_ssh_diagnostics_reach_the_cli_as_they_reach_the_api(monkeypatch) -> None:
    _master(monkeypatch, before=False, after=False, rc=255)
    monkeypatch.setattr(ssh, "check", lambda spec: subprocess.CompletedProcess([], 255, "", "Host key verification failed.\n"))
    r = _cli("-t", "c", "login", "-v")
    with pytest.raises(api.LoginError) as info:
        api.login("c", verbose=1)
    assert info.value.diagnostics in _norm(r)
