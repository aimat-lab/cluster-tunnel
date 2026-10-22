"""Job records: `run` writes them, `jobs --json` attaches them (ssh mocked)."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from cluster_tunnel import api, facts, jobs, notes, records
from cluster_tunnel.cli import cli
from cluster_tunnel.constants import PROBE_TIMEOUT, ExitCode
from tests.test_cli import _cfg, _norm, _patch_jobs

FACTS_OUTPUT = (
    "slurm_cluster=haic\n"
    "job_name=train\n"
    "workdir=/work/repo\n"
    "commit=0123456789abcdef0123456789abcdef01234567\n"
    "branch=main\n"
    "pushed=no\n"
    "dirty= M models/train.py\n"
)


def _version(record: dict) -> str:
    """The version records.sh gives a record that FakeCluster lists as json.dumps(record)."""
    return hashlib.sha256(json.dumps(record).encode("utf-8")).hexdigest()


class FakeCluster:
    """Answers `feed_script` for records.sh like a cluster would."""

    def __init__(self, facts_rc: int = 0, known_refs: tuple[str, ...] = (), submitted_ago: int = 2) -> None:
        self.facts_rc = facts_rc
        self.submitted_ago = submitted_ago
        self.known_refs = known_refs
        self.written: dict[str, dict] = {}
        self.note_checks: list[list] = []
        self.update_calls = 0
        #: Called before each update call, to change records "on another machine".
        self.meanwhile = lambda: None

    def feed_script(self, spec, script_path: Path, args: list, timeout=None) -> subprocess.CompletedProcess:
        assert script_path == records.SCRIPT_PATH
        assert timeout == PROBE_TIMEOUT  # a hung cluster must not hang ctun
        command, *rest = args
        if command == "refs":
            self.note_checks.append(rest)
            return subprocess.CompletedProcess([], 0, "\n".join(self.known_refs), "")
        if command == "facts":
            if self.facts_rc:
                return subprocess.CompletedProcess([], self.facts_rc, "", "squeue: not found")
            return subprocess.CompletedProcess([], 0, f"{FACTS_OUTPUT}submitted_ago={self.submitted_ago}\n", "")
        if command == "write":
            job_id, payload = rest
            self.written[job_id] = json.loads(base64.b64decode(payload))
            return subprocess.CompletedProcess([], 0, "", "")
        if command == "update":
            self.update_calls += 1
            self.meanwhile()
            said = []
            for job_id, version, payload in zip(rest[0::3], rest[1::3], rest[2::3]):
                current = self.written.get(job_id)
                if version != ("absent" if current is None else _version(current)):
                    said.append(f"changed {job_id}")
                    continue
                self.written[job_id] = json.loads(base64.b64decode(payload))
                said.append(f"stored {job_id}")
            return subprocess.CompletedProcess([], 0, "".join(f"{line}\n" for line in said), "")
        lines = "\n".join(json.dumps(record) for record in self.written.values())
        return subprocess.CompletedProcess([], 0, lines, "")


def _patch_run(monkeypatch, cluster: FakeCluster, output: str, rc: int = 0) -> None:
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: object())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: True)

    def fake_run(spec, tokens, tty=False, on_line=None):
        if on_line is not None:
            for line in output.splitlines():
                on_line(line)
        return rc

    monkeypatch.setattr("cluster_tunnel.ssh.run", fake_run)
    monkeypatch.setattr("cluster_tunnel.ssh.feed_script", cluster.feed_script)
    monkeypatch.setattr("cluster_tunnel.cmdlog.record", lambda name, tokens: "0")
    monkeypatch.setattr("cluster_tunnel.cmdlog.record_jobs", lambda name, entry_id, job_ids: None)


def test_run_writes_a_record_with_the_facts(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Run ID: x\nSubmitted batch job 4822010\n")
    r = CliRunner().invoke(
        cli, ["-t", "localhost", "run", "--project", "htfm", "--note", "lr sweep", "--", "./submit.sh"]
    )
    assert r.exit_code == 0, r.output
    assert "recorded job 4822010" in r.output
    record = cluster.written["4822010"]
    assert record["schema_version"] == records.SCHEMA_VERSION
    assert record["slurm_cluster"] == "haic"
    assert record["job_name"] == "train"
    assert record["project"] == "htfm" and record["note"] == "lr sweep"
    assert record["command"] == ["./submit.sh"]
    assert record["code"] == {
        "workdir": "/work/repo",
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "branch": "main",
        "pushed": False,
        "uncommitted": [" M models/train.py"],
        "uncommitted_omitted": 0,
    }
    assert record["submitted_at"] and record["submitted_from"]


def test_api_run_returns_the_records_it_wrote(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    result = api.run("localhost", ["sbatch", "job.sh"], project="htfm")
    assert [record.job_id for record in result.records] == ["7"]
    assert result.records[0].project == "htfm" and result.warnings == []


def test_run_without_a_job_id_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "JOBID PARTITION\n", rc=3)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "squeue", "--me"])
    assert r.exit_code == 3
    assert cluster.written == {}


def test_a_failing_fact_warns_but_still_records(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(facts_rc=1)
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "sbatch", "job.sh"])
    assert r.exit_code == 0
    assert "could not collect the facts of job 7" in r.output
    assert cluster.written["7"]["code"]["commit"] is None
    assert cluster.written["7"]["slurm_cluster"] is None


def test_a_job_older_than_the_command_gets_no_record(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(submitted_ago=3 * 3600)
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--", "cat", "submit.log"])
    assert r.exit_code == 0 and cluster.written == {}
    assert "ctun:" not in r.output  # nothing was asked for, so nothing to warn about
    result = api.run("localhost", ["cat", "submit.log"], note="lr sweep")
    assert result.job_ids == ["7"] and result.records == []
    assert result.warnings == ["no record for job 7: it was submitted before this command"]


def test_records_off_writes_no_record(tmp_path: Path, monkeypatch) -> None:
    config = _cfg(tmp_path, monkeypatch)
    config.write_text("clusters:\n  localhost:\n    host: localhost\n    job_records: false\n", encoding="utf-8")
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--note", "x", "--", "sbatch", "job.sh"])
    assert r.exit_code == 0
    assert "no job record written" in r.output
    assert cluster.written == {}


def test_a_tty_run_writes_no_record(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    monkeypatch.setattr("cluster_tunnel.ssh.run", lambda spec, tokens, tty=False: 0)
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--tty", "--project", "htfm", "--", "sbatch", "job.sh"])
    assert r.exit_code == 0
    assert "no job record written (a TTY hides the output)" in r.output
    assert cluster.written == {}


def test_from_json_ignores_unknown_keys_and_fills_missing() -> None:
    record = records.JobRecord.from_json('{"job_id": 5, "future_field": [1], "project": "p"}')
    assert record.job_id == "5" and record.project == "p"
    assert record.note is None and record.code == {} and record.command == []


def test_facts_count_the_uncommitted_files_left_out() -> None:
    parsed = facts.parse("commit=abc\ndirty= M a.py\ndirty_omitted=523\n")
    assert parsed.uncommitted == [" M a.py"] and parsed.uncommitted_omitted == 523
    assert facts.parse("workdir=/x\n").uncommitted_omitted is None  # no repo, nothing known


def test_submitted_job_ids_reads_every_submission() -> None:
    output = "Submitted batch job 1\nnoise Submitted batch job 9\nSubmitted batch job 2\n"
    assert jobs.submitted_job_ids(output) == ["1", "2"]


def test_jobs_json_attaches_records(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel.jobs import Job

    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch, jobs=[Job("active", "101", "RUNNING", "1:00", "2:00:00", "gpu", "1", "N/A", "train")])
    stored = [
        records.JobRecord.from_json('{"job_id": "101", "project": "htfm"}'),
        records.JobRecord.from_json('{"job_id": "99", "project": "htfm"}'),
    ]
    monkeypatch.setattr("cluster_tunnel.records.list_recent", lambda spec, since, ids: stored)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "--json"])
    entry = json.loads(r.output)["clusters"][0]
    assert entry["jobs"][0]["record"]["project"] == "htfm"
    assert [record["job_id"] for record in entry["records_without_job"]] == ["99"]
    assert entry["records_error"] is None


def test_record_keys_lead_from_a_task_to_its_array() -> None:
    assert records.keys("123") == ["123"]
    assert records.keys("123_4") == ["123_4", "123"]
    assert records.keys("123_[5-9]") == ["123"]
    assert records.keys("123+0") == ["123"]
    assert records.keys("N/A") == []


def test_jobs_json_gives_an_arrays_tasks_its_record(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel.jobs import Job

    _cfg(tmp_path, monkeypatch)
    listed = [
        Job("active", "123_1", "RUNNING", "1:00", "2:00:00", "gpu", "1", "N/A", "sweep"),
        Job("active", "123_2", "RUNNING", "1:00", "2:00:00", "gpu", "1", "N/A", "sweep"),
        Job("active", "123_[3-9]", "PENDING", "0:00", "2:00:00", "gpu", "1", "N/A", "sweep"),
    ]
    _patch_jobs(monkeypatch, jobs=listed)
    asked = []

    def list_recent(spec, since, ids):
        asked.extend(ids)
        return [
            records.JobRecord.from_json('{"job_id": "123", "project": "htfm"}'),
            records.JobRecord.from_json('{"job_id": "123_2", "project": "htfm/fix"}'),
        ]

    monkeypatch.setattr("cluster_tunnel.records.list_recent", list_recent)
    entry = json.loads(CliRunner().invoke(cli, ["-t", "localhost", "jobs", "--json"]).output)["clusters"][0]
    assert [job["record"]["project"] for job in entry["jobs"]] == ["htfm", "htfm/fix", "htfm"]
    assert entry["records_without_job"] == []
    assert asked == ["123_1", "123", "123_2"]


def test_jobs_json_reports_unreadable_records_and_still_lists_the_jobs(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel.jobs import Job

    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch, jobs=[Job("active", "101", "RUNNING", "1:00", "2:00:00", "gpu", "1", "N/A", "train")])

    def unreadable(spec, since, ids):
        raise RuntimeError("could not read the job records: scontrol: not found")

    monkeypatch.setattr("cluster_tunnel.records.list_recent", unreadable)
    entry = json.loads(CliRunner().invoke(cli, ["-t", "localhost", "jobs", "--json"]).output)["clusters"][0]
    assert [job["jobid"] for job in entry["jobs"]] == ["101"]
    assert entry["records_error"].endswith("scontrol: not found")


def test_api_jobs_reads_no_records_unless_asked(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch)
    monkeypatch.setattr("cluster_tunnel.records.list_recent", lambda *a: (_ for _ in ()).throw(AssertionError))
    [entry] = api.jobs("localhost")
    assert entry.records is None and "records_without_job" not in entry.as_dict()


# --- note references --------------------------------------------------------------


def test_references_finds_markers_once() -> None:
    note = "rerun of job:1695485 after commit:541b4cb; see job:1695485 and commit:zz"
    assert notes.references(note) == ["job:1695485", "commit:541b4cb"]


def test_note_with_an_unknown_job_is_refused_before_running(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(known_refs=("job:7",))
    _patch_run(monkeypatch, cluster, "Submitted batch job 8\n")
    note = "after job:7 and job:6, at commit:abcdef1"
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--project", "htfm", "--note", note, "--", "sbatch", "x"])
    assert r.exit_code == ExitCode.NOTE_REFERENCE_INVALID
    assert "job:6" in r.output and "note_reference_invalid" in r.output
    assert "8" not in cluster.written  # nothing ran, so nothing was recorded
    assert cluster.note_checks == [["--", "job:7", "job:6"]]  # commits wait for the submission


def test_note_with_an_unknown_commit_is_stored_with_a_warning(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["1"] = {"job_id": "1", "project": "htfm", "code": {"workdir": "/work/old"}}
    cluster.written["2"] = {"job_id": "2", "project": "other", "code": {"workdir": "/work/other"}}
    _patch_run(monkeypatch, cluster, "Submitted batch job 8\n")
    note = "at commit:abcdef1"
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--project", "htfm", "--note", note, "--", "sbatch", "x"])
    assert r.exit_code == 0, r.output
    assert cluster.written["8"]["note"] == note
    assert "commits are in neither the job's repo nor its project's: commit:abcdef1" in _norm(r)
    # The new job's repo, then those of the project's records.
    assert cluster.note_checks == [["/work/repo", "/work/old", "--", "commit:abcdef1"]]


def test_the_first_job_of_a_project_may_name_a_commit_of_its_repo(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(known_refs=("commit:abcdef1",))
    _patch_run(monkeypatch, cluster, "Submitted batch job 8\n")
    result = api.run("localhost", ["sbatch", "x"], project="new", note="at commit:abcdef1")
    assert result.warnings == [] and result.records[0].note == "at commit:abcdef1"
    assert cluster.note_checks == [["/work/repo", "--", "commit:abcdef1"]]


def test_api_run_raises_on_an_unknown_reference(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_run(monkeypatch, FakeCluster(), "Submitted batch job 8\n")
    try:
        api.run("localhost", ["sbatch", "x"], note="after job:7")
    except api.NoteReferenceInvalid as exc:
        assert exc.unknown == ["job:7"] and exc.exit_code == ExitCode.NOTE_REFERENCE_INVALID
    else:
        raise AssertionError("an unknown reference must stop the run")


def test_note_with_known_references_runs(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(known_refs=("job:7",))
    _patch_run(monkeypatch, cluster, "Submitted batch job 8\n")
    r = CliRunner().invoke(cli, ["-t", "localhost", "run", "--note", "rerun of job:7", "--", "sbatch", "x"])
    assert r.exit_code == 0, r.output
    assert cluster.written["8"]["note"] == "rerun of job:7"
    assert cluster.note_checks == [["--", "job:7"]]


# --- editing a record (ctun jobs edit) ---------------------------------------------


def test_a_new_note_is_unconfirmed_and_logged() -> None:
    current = {"job_id": "7", "note": "old", "note_confirmed": True, "code": {"commit": "abc"}, "future": 1}
    record = records.edited(current, "7", project=None, note="new", confirm_note=False)
    assert record["note"] == "new" and record["note_confirmed"] is False
    assert record["code"] == {"commit": "abc"} and record["future"] == 1
    (change,) = record["changes"]
    assert change["note"] == ["old", "new"] and change["note_confirmed"] == [True, False]
    assert change["at"] and change["from"]


def test_a_missing_confirmation_counts_as_unconfirmed() -> None:
    record = records.edited({"job_id": "7", "note": "old"}, "7", project=None, note="new", confirm_note=False)
    (change,) = record["changes"]
    assert "note_confirmed" not in change and record["note_confirmed"] is False


def test_empty_values_clear_and_confirm_needs_a_note() -> None:
    current = {"job_id": "7", "project": "htfm", "note": "x"}
    record = records.edited(current, "7", project="", note="", confirm_note=False)
    assert record["project"] is None and record["note"] is None
    try:
        records.edited(record, "7", project=None, note=None, confirm_note=True)
    except ValueError as exc:
        assert "no note to confirm" in str(exc)
    else:
        raise AssertionError("confirming a missing note must fail")


def test_an_edit_that_changes_nothing_returns_none() -> None:
    current = {"job_id": "7", "project": "htfm", "note": "x", "note_confirmed": True}
    assert records.edited(current, "7", project="htfm", note="x", confirm_note=True) is None


def _patch_job(monkeypatch, cluster: FakeCluster) -> None:
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: object())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: True)
    monkeypatch.setattr("cluster_tunnel.ssh.feed_script", cluster.feed_script)


def test_jobs_edit_prints_the_resulting_record(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["7"] = {"job_id": "7", "project": "htfm"}
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "--project", "other"])
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout) == [cluster.written["7"]] and cluster.written["7"]["project"] == "other"


def test_api_edit_job_reports_whether_it_changed_the_record(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["7"] = {"job_id": "7", "project": "htfm"}
    _patch_job(monkeypatch, cluster)
    edit = api.edit_job("localhost", "7", project="other")
    assert edit.changed and edit.record["project"] == "other"
    assert not api.edit_job("localhost", "7", project="other").changed


def test_job_edit_gives_an_old_job_a_record_without_facts(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(known_refs=("job:7",))
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "--project", "htfm/mh", "--note", "lr 3e-4", "--confirm-note"])
    assert r.exit_code == 0, r.output
    record = cluster.written["7"]
    assert record["project"] == "htfm/mh" and record["note"] == "lr 3e-4" and record["note_confirmed"] is True
    assert "submitted_at" not in record and len(record["changes"]) == 1


def test_job_edit_refuses_an_unknown_job(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "--project", "htfm"])
    assert r.exit_code != 0 and "unknown" in r.output
    assert cluster.written == {}


def test_job_edit_refuses_what_is_no_job_id(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(known_refs=("job:5131",))
    _patch_job(monkeypatch, cluster)
    for bad in ("5131x", "../../x", "5131_", ""):
        r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", bad, "--project", "htfm"])
        assert r.exit_code != 0 and "is not a Slurm job ID" in r.output, bad
    assert cluster.written == {} and cluster.note_checks == []


def test_a_tasks_first_record_starts_from_its_arrays(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    array = {"job_id": "5131", "project": "htfm", "note": "sweep", "code": {"commit": "abc"}, "changes": [{"at": 1}]}
    cluster.written["5131"] = array
    _patch_job(monkeypatch, cluster)
    assert api.edit_job("localhost", "5131_4", note="the diverging one").changed
    task = cluster.written["5131_4"]
    assert task["project"] == "htfm" and task["code"] == {"commit": "abc"} and task["note"] == "the diverging one"
    assert [change["note"] for change in task["changes"]] == [["sweep", "the diverging one"]]
    assert cluster.written["5131"] == array and cluster.note_checks == []  # no need to ask Slurm


def test_job_edit_takes_an_array_task(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(known_refs=("job:5131_4",))
    _patch_job(monkeypatch, cluster)
    assert api.edit_job("localhost", "5131_4", project="htfm").changed
    assert cluster.written["5131_4"]["project"] == "htfm"


def test_job_edit_checks_the_note_references(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["7"] = {"job_id": "7", "project": "htfm", "code": {"workdir": "/work/repo"}}
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "--note", "after job:5"])
    assert r.exit_code == ExitCode.NOTE_REFERENCE_INVALID
    assert "Not saved" in r.output and "note" not in cluster.written["7"]


def test_job_edit_looks_for_commits_in_the_jobs_own_repo(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster(known_refs=("commit:abcdef1",))
    cluster.written["7"] = {"job_id": "7", "code": {"workdir": "/work/repo"}}
    _patch_job(monkeypatch, cluster)
    assert api.edit_job("localhost", "7", note="at commit:abcdef1").changed
    assert cluster.note_checks == [["/work/repo", "--", "commit:abcdef1"]]


def test_jobs_edit_without_a_change_is_a_usage_error(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7"])
    assert r.exit_code == 2 and "--project, --note or --confirm-note" in _norm(r)


def test_plain_jobs_and_jobs_list_print_the_same_overview(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch)
    plain = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "--json"])
    listed = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "list", "--json"])
    assert plain.exit_code == listed.exit_code == 0
    assert json.loads(plain.output) == json.loads(listed.output)


def test_jobs_options_before_list_count_for_list(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_jobs(monkeypatch)
    windows = []
    monkeypatch.setattr(
        "cluster_tunnel.jobs.query",
        lambda spec, user, since_minutes: windows.append(since_minutes) or jobs.Overview("haic", []),
    )
    before = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "-s", "2d", "--json", "list"])
    after = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "list", "-s", "2d", "--json"])
    own = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "-s", "2d", "list", "-s", "1h", "--json"])
    assert before.exit_code == after.exit_code == own.exit_code == 0, before.output
    assert json.loads(before.output) == json.loads(after.output)
    assert windows == [2 * 24 * 60, 2 * 24 * 60, 60]  # list's own --since wins


def test_jobs_options_before_edit_are_refused(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["7"] = {"job_id": "7"}
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "--json", "edit", "7", "--project", "x"])
    assert r.exit_code == 2 and "takes no --since or --json" in _norm(r)
    assert "project" not in cluster.written["7"]


def test_jobs_edit_gives_several_jobs_the_same_change(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["7"] = {"job_id": "7", "project": "old"}
    cluster.written["8"] = {"job_id": "8", "project": "new"}
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "8", "--project", "new"])
    assert r.exit_code == 0, r.output
    assert [record["project"] for record in json.loads(r.stdout)] == ["new", "new"]
    assert "updated job 7" in r.stderr and "job 8 already has these values" in r.stderr


def test_jobs_edit_changes_no_job_when_one_is_unknown(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written["7"] = {"job_id": "7"}
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "jobs", "edit", "7", "999", "998", "--project", "new"])
    assert r.exit_code != 0 and "Jobs 999, 998 are unknown" in _norm(r)
    assert cluster.written == {"7": {"job_id": "7"}} and cluster.update_calls == 0


def test_jobs_edit_reads_and_stores_many_jobs_in_one_call_each(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {str(job_id): {"job_id": str(job_id)} for job_id in range(100, 140)}
    _patch_job(monkeypatch, cluster)
    calls = []
    feed = cluster.feed_script
    monkeypatch.setattr(
        "cluster_tunnel.ssh.feed_script", lambda spec, path, args, timeout=None: calls.append(args[0]) or feed(
            spec, path, args, timeout
        )
    )
    edits = api.edit_jobs("localhost", [str(job_id) for job_id in range(100, 140)], project="htfm")
    assert all(edit.changed for edit in edits) and calls == ["list", "update"]
    assert {record["project"] for record in cluster.written.values()} == {"htfm"}


def test_update_many_splits_the_records_into_batches_that_fit_a_command_line(monkeypatch) -> None:
    cluster = FakeCluster()
    monkeypatch.setattr("cluster_tunnel.ssh.feed_script", cluster.feed_script)
    many = [({"job_id": str(job_id), "note": "x" * 1000}, "absent") for job_id in range(100)]
    done = records.update_many(object(), many)
    assert len(cluster.written) == 100 and cluster.update_calls > 1
    assert done.stored == [str(job_id) for job_id in range(100)] and done.changed == []


def test_update_many_names_what_it_stored_before_a_failure(monkeypatch) -> None:
    calls = []

    def flaky(spec, script_path, args, timeout=None):
        job_ids = args[1::3]
        calls.append(job_ids)
        if len(calls) == 1:
            return subprocess.CompletedProcess([], 0, "".join(f"stored {i}\n" for i in job_ids), "")
        # The second call stores its first record, then fails.
        return subprocess.CompletedProcess([], 5, f"stored {job_ids[0]}\n", "the record is not valid base64")

    monkeypatch.setattr("cluster_tunnel.ssh.feed_script", flaky)
    many = [({"job_id": str(job_id), "note": "x" * 7000}, "absent") for job_id in range(4)]
    try:
        records.update_many(object(), many)
    except records.UpdateError as exc:
        assert calls == [["0", "1"], ["2", "3"]] and exc.stored == ["0", "1", "2"]
        assert "could not store the records of jobs 3: the record is not valid base64" in str(exc)
    else:
        raise AssertionError("a failed call must raise")


# --- listing projects (ctun projects list) ---------------------------------------


def test_projects_list_counts_the_jobs_of_every_record(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {
        "7": {"job_id": "7", "project": "htfm/mh"},
        "8": {"job_id": "8", "project": "htfm/mh"},
        "9": {"job_id": "9", "project": "eval"},
        "10": {"job_id": "10", "project": None},
    }
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "projects", "list", "--json"])
    assert r.exit_code == 0, r.output
    (entry,) = json.loads(r.stdout)["clusters"]
    assert entry == {
        "cluster": "localhost",
        "live": True,
        "job_records": True,
        "error": None,
        "projects": [{"project": "eval", "jobs": 1}, {"project": "htfm/mh", "jobs": 2}],
    }
    assert [item.as_dict() for item in api.projects("localhost")] == [entry]


def test_projects_list_counts_an_array_once(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {
        "123": {"job_id": "123", "project": "htfm"},
        "123_1": {"job_id": "123_1", "project": "htfm"},
        "123_2": {"job_id": "123_2", "project": "htfm/fix"},
        "../x": {"job_id": "../x", "project": "htfm"},
    }
    _patch_job(monkeypatch, cluster)
    [entry] = api.projects("localhost")
    assert entry.projects == [{"project": "htfm", "jobs": 1}, {"project": "htfm/fix", "jobs": 1}]


def test_projects_list_skips_a_down_tunnel(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_job(monkeypatch, FakeCluster())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: False)
    r = CliRunner().invoke(cli, ["-t", "localhost", "projects", "list"])
    assert r.exit_code == 0 and "tunnel down" in r.output


def test_projects_list_marks_a_cluster_without_records(tmp_path: Path, monkeypatch) -> None:
    config = _cfg(tmp_path, monkeypatch)
    config.write_text("clusters:\n  localhost:\n    host: localhost\n    job_records: false\n", encoding="utf-8")
    _patch_job(monkeypatch, FakeCluster())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: (_ for _ in ()).throw(AssertionError))
    r = CliRunner().invoke(cli, ["-t", "localhost", "projects", "list", "--json"])
    (entry,) = json.loads(r.stdout)["clusters"]
    assert entry["job_records"] is False and entry["error"] is None and entry["projects"] == []
    assert entry["live"] is None  # not checked: the cluster is skipped anyway


# --- renaming a project (ctun projects rename) ------------------------------------


def test_projects_rename_renames_the_nested_projects_too(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {
        "7": {"job_id": "7", "project": "htfm"},
        "8": {"job_id": "8", "project": "htfm/mh"},
        "9": {"job_id": "9", "project": "htfmx"},
        "10": {"job_id": "10", "project": None},
    }
    _patch_job(monkeypatch, cluster)
    r = CliRunner().invoke(cli, ["-t", "localhost", "projects", "rename", "htfm", "archive/htfm", "--json"])
    assert r.exit_code == 0, r.output
    (entry,) = json.loads(r.stdout)["clusters"]
    assert entry == {"cluster": "localhost", "live": True, "job_records": True, "error": None, "renamed": ["7", "8"]}
    assert cluster.update_calls == 1
    projects = {job_id: record["project"] for job_id, record in cluster.written.items()}
    assert projects == {"7": "archive/htfm", "8": "archive/htfm/mh", "9": "htfmx", "10": None}
    assert cluster.written["8"]["changes"][-1]["project"] == ["htfm/mh", "archive/htfm/mh"]


def test_projects_rename_refuses_an_empty_name(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_job(monkeypatch, FakeCluster())
    r = CliRunner().invoke(cli, ["-t", "localhost", "projects", "rename", "htfm", "/"])
    assert r.exit_code == 2 and "the new project name is empty" in _norm(r)
    r = CliRunner().invoke(cli, ["-t", "localhost", "projects", "rename", "", "x"])
    assert r.exit_code == 2 and "the project to rename is empty" in _norm(r)


def test_a_rename_into_a_project_inside_old_is_safe_to_run_again(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {
        "7": {"job_id": "7", "project": "htfm"},
        "8": {"job_id": "8", "project": "htfm/mh"},
        "9": {"job_id": "9", "project": "htfm/oldish"},
    }
    _patch_job(monkeypatch, cluster)
    renamed = {"7": "htfm/old", "8": "htfm/old/mh", "9": "htfm/old/oldish"}
    assert api.rename_project("htfm", "htfm/old", "localhost")[0].renamed == ["7", "8", "9"]
    assert {job_id: record["project"] for job_id, record in cluster.written.items()} == renamed
    assert api.rename_project("htfm", "htfm/old", "localhost")[0].renamed == []
    assert {job_id: record["project"] for job_id, record in cluster.written.items()} == renamed


def test_damaged_records_neither_stop_nor_break_a_rename(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {
        "a": {"project": "htfm"},
        "b": {"job_id": "../x", "project": "htfm"},
        "c": {"job_id": True, "project": "htfm"},
        "d": {"job_id": "8", "project": 5},
        "7": {"job_id": 7, "project": "htfm"},
    }
    _patch_job(monkeypatch, cluster)
    assert api.projects("localhost")[0].error is None  # project 5 is not counted, and nothing breaks
    [entry] = api.rename_project("htfm", "eval", "localhost")
    assert entry.error is None and entry.renamed == ["7"]
    assert cluster.written["7"]["project"] == "eval" and cluster.written["a"] == {"project": "htfm"}


def test_a_rename_keeps_an_edit_made_meanwhile_on_another_machine(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {"7": {"job_id": "7", "project": "htfm", "note": "x"}, "8": {"job_id": "8", "project": "htfm"}}
    _patch_job(monkeypatch, cluster)

    def confirm_once():
        cluster.meanwhile = lambda: None
        cluster.written["7"] = {**cluster.written["7"], "note_confirmed": True}

    cluster.meanwhile = confirm_once
    [entry] = api.rename_project("htfm", "eval", "localhost")
    assert entry.error is None and sorted(entry.renamed) == ["7", "8"] and cluster.update_calls == 2
    assert cluster.written["7"]["project"] == "eval" and cluster.written["7"]["note_confirmed"] is True


def test_a_rename_gives_up_on_a_record_that_keeps_changing(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {"7": {"job_id": "7", "project": "htfm"}, "8": {"job_id": "8", "project": "htfm"}}
    _patch_job(monkeypatch, cluster)
    edits = iter(range(100))
    cluster.meanwhile = lambda: cluster.written["7"].update(note=f"edit {next(edits)}")
    [entry] = api.rename_project("htfm", "eval", "localhost")
    assert entry.renamed == ["8"] and "kept changing while they were renamed: 7" in entry.error


def test_a_failed_rename_names_what_it_stored(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {"7": {"job_id": "7", "project": "htfm"}}
    _patch_job(monkeypatch, cluster)

    def fail(spec, pairs):
        raise records.UpdateError("could not store the records of jobs 8: timed out", ["7"])

    monkeypatch.setattr("cluster_tunnel.records.update_many", fail)
    [entry] = api.rename_project("htfm", "eval", "localhost")
    assert entry.renamed == ["7"] and entry.error.endswith("timed out")


def test_jobs_edit_keeps_an_edit_made_meanwhile_on_another_machine(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {"7": {"job_id": "7", "note": "x"}}
    _patch_job(monkeypatch, cluster)

    def confirm_once():
        cluster.meanwhile = lambda: None
        cluster.written["7"] = {**cluster.written["7"], "note_confirmed": True}

    cluster.meanwhile = confirm_once
    edit = api.edit_job("localhost", "7", project="htfm")
    assert edit.changed and cluster.update_calls == 2
    assert cluster.written["7"]["project"] == "htfm" and cluster.written["7"]["note_confirmed"] is True


def test_project_names_lose_stray_slashes_and_blanks() -> None:
    assert records.project_name(" archive//htfm/ ") == "archive/htfm"
    assert records.project_name("a / b") == "a/b"
    assert records.project_name("/") == ""


def test_projects_rename_takes_the_names_as_stored(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    cluster.written = {"7": {"job_id": "7", "project": "htfm"}, "8": {"job_id": "8", "project": "htfm/mh"}}
    _patch_job(monkeypatch, cluster)
    [entry] = api.rename_project("htfm/", "archive/", "localhost")
    assert entry.renamed == ["7", "8"]
    assert [record["project"] for record in cluster.written.values()] == ["archive", "archive/mh"]


def test_run_and_edit_store_the_project_name_as_stored(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    cluster = FakeCluster()
    _patch_run(monkeypatch, cluster, "Submitted batch job 7\n")
    assert api.run("localhost", ["sbatch", "x"], project="htfm//mh/").records[0].project == "htfm/mh"
    assert api.edit_job("localhost", "7", project=" eval/ ").record["project"] == "eval"


def test_projects_rename_on_a_down_target_needs_a_login(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_job(monkeypatch, FakeCluster())
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: False)
    r = CliRunner().invoke(cli, ["-t", "localhost", "projects", "rename", "htfm", "eval", "--json"])
    assert r.exit_code == ExitCode.LOGIN_REQUIRED and "login_required" in r.output


def _two_clusters(tmp_path: Path, monkeypatch, cluster: FakeCluster, live: set[str]) -> None:
    config = _cfg(tmp_path, monkeypatch)
    config.write_text(
        "clusters:\n  a:\n    host: a\n  b:\n    host: b\n  c:\n    host: c\n    job_records: false\n",
        encoding="utf-8",
    )
    _patch_job(monkeypatch, cluster)
    monkeypatch.setattr("cluster_tunnel.ssh.conn_spec", lambda config, name: name)
    monkeypatch.setattr("cluster_tunnel.ssh.is_live", lambda spec: spec in live)


def test_projects_rename_sweep_notes_a_down_cluster_and_succeeds(tmp_path: Path, monkeypatch) -> None:
    cluster = FakeCluster()
    cluster.written = {"7": {"job_id": "7", "project": "htfm"}}
    _two_clusters(tmp_path, monkeypatch, cluster, live={"a"})
    r = CliRunner().invoke(cli, ["projects", "rename", "htfm", "eval"])
    assert r.exit_code == 0, r.output
    out = _norm(r)
    assert "tunnel down" in out and "job_records is off" in out and "1 jobs" in out


def test_projects_rename_fails_when_a_cluster_fails(tmp_path: Path, monkeypatch) -> None:
    cluster = FakeCluster()
    cluster.written = {"7": {"job_id": "7", "project": "htfm"}}
    _two_clusters(tmp_path, monkeypatch, cluster, live={"a", "b"})

    def fail(spec, pairs):
        raise records.UpdateError("could not store the records of jobs 7: timed out", [])

    monkeypatch.setattr("cluster_tunnel.records.update_many", fail)
    r = CliRunner().invoke(cli, ["projects", "rename", "htfm", "eval", "--json"])
    assert r.exit_code == 1
    assert [entry["error"] for entry in json.loads(r.stdout)["clusters"]][:2] == [
        "could not store the records of jobs 7: timed out"
    ] * 2


def test_projects_rename_on_a_target_without_records_fails(tmp_path: Path, monkeypatch) -> None:
    _two_clusters(tmp_path, monkeypatch, FakeCluster(), live={"a", "b", "c"})
    r = CliRunner().invoke(cli, ["-t", "c", "projects", "rename", "htfm", "eval"])
    assert r.exit_code == 1 and "job_records is off" in r.output
