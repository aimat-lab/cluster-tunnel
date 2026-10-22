"""The bundled records.sh runs for real against fake Slurm tools on PATH."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "cluster_tunnel" / "scripts"

_FAKE_SCONTROL = "#!/bin/sh\nprintf 'Configuration data\\nClusterName             = haic\\n'\n"


def _fake_tools(tmp_path: Path, workdir: str = "") -> Path:
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    tools = {
        "scontrol": _FAKE_SCONTROL,
        # squeue -h -j <id> -o '%j' | '%Z': the last argument picks the field.
        "squeue": (
            "#!/bin/sh\n"
            'for last; do :; done\n'
            'case "$last" in %j) echo "train job" ;; %V) echo "2020-01-01T00:00:00" ;;'
            ' %Z) echo "' + workdir + '" ;; esac\n'
        ),
        "sacct": "#!/bin/sh\nexit 0\n",
    }
    for name, body in tools.items():
        tool = binp / name
        tool.write_text(body, encoding="utf-8", newline="\n")
        tool.chmod(0o755)
    return binp


def _run(bash: str, tmp_path: Path, script: str, *args: str, workdir: str = "") -> subprocess.CompletedProcess:
    binp = _fake_tools(tmp_path, workdir)
    env = dict(os.environ, PATH=f"{binp}{os.pathsep}{os.environ['PATH']}", HOME=str(tmp_path / "home"))
    return subprocess.run(
        [bash, str(SCRIPTS / script), *args],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )


def _b64(document: dict) -> str:
    return base64.b64encode(json.dumps(document).encode("utf-8")).decode("ascii")


def test_write_then_list_by_id_and_window(tmp_path: Path, bash: str) -> None:
    assert _run(bash, tmp_path, "records.sh", "write", "101", _b64({"job_id": "101"})).returncode == 0
    assert _run(bash, tmp_path, "records.sh", "write", "102", _b64({"job_id": "102"})).returncode == 0
    stored = tmp_path / "home" / ".ctun" / "jobs" / "haic" / "101.json"
    assert json.loads(stored.read_text(encoding="utf-8")) == {"job_id": "101"}

    by_id = _run(bash, tmp_path, "records.sh", "list", "0", "102", "999")
    assert [json.loads(line)["job_id"] for line in by_id.stdout.splitlines()] == ["102"]
    assert by_id.returncode == 0  # a last job without a record is no failure

    in_window = _run(bash, tmp_path, "records.sh", "list", "60", "101")
    assert sorted(json.loads(line)["job_id"] for line in in_window.stdout.splitlines()) == ["101", "102"]


def test_write_never_overwrites(tmp_path: Path, bash: str) -> None:
    _run(bash, tmp_path, "records.sh", "write", "101", _b64({"note": "first"}))
    again = _run(bash, tmp_path, "records.sh", "write", "101", _b64({"note": "second"}))
    assert again.returncode == 4
    lines = _run(bash, tmp_path, "records.sh", "all").stdout.splitlines()
    assert [json.loads(line)["note"] for line in lines] == ["first"]


def test_list_without_records_is_empty(tmp_path: Path, bash: str) -> None:
    result = _run(bash, tmp_path, "records.sh", "list", "60")
    assert result.returncode == 0 and result.stdout == ""


def test_update_overwrites_a_record_and_creates_a_missing_one(tmp_path: Path, bash: str) -> None:
    assert _run(bash, tmp_path, "records.sh", "write", "101", _b64({"job_id": "101"})).returncode == 0
    assert _run(bash, tmp_path, "records.sh", "update", "101", _b64({"job_id": "101", "note": "x"})).returncode == 0
    assert _run(bash, tmp_path, "records.sh", "update", "102", _b64({"job_id": "102"})).returncode == 0
    listed = _run(bash, tmp_path, "records.sh", "list", "0", "101", "102").stdout.splitlines()
    assert [json.loads(line) for line in listed] == [{"job_id": "101", "note": "x"}, {"job_id": "102"}]
    folder = tmp_path / "home" / ".ctun" / "jobs" / "haic"
    assert sorted(path.name for path in folder.iterdir()) == ["101.json", "102.json"]  # no temp files left


def test_a_record_is_stored_only_under_a_job_id(tmp_path: Path, bash: str) -> None:
    for command in ("write", "update"):
        result = _run(bash, tmp_path, "records.sh", command, "../../x", _b64({"job_id": "x"}))
        assert result.returncode == 2 and "not a job ID" in result.stderr
    assert not (tmp_path / "home" / ".ctun" / "x.json").exists()
    assert _run(bash, tmp_path, "records.sh", "update", "101_4", _b64({"job_id": "101_4"})).returncode == 0
    listed = _run(bash, tmp_path, "records.sh", "list", "0", "../101_4", "101_4").stdout.splitlines()
    assert [json.loads(line)["job_id"] for line in listed] == ["101_4"]


def test_all_with_an_empty_folder_is_empty(tmp_path: Path, bash: str) -> None:
    (tmp_path / "home" / ".ctun" / "jobs" / "haic").mkdir(parents=True)
    result = _run(bash, tmp_path, "records.sh", "all")
    assert result.returncode == 0 and result.stdout == ""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _posix_path(bash: str, path: Path) -> str:
    return subprocess.run(
        [bash, "-c", f"cd '{path.as_posix()}' && pwd"], capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    (repo / "train.py").write_text("print()\n", encoding="utf-8")
    return repo


def test_facts_report_job_and_code(tmp_path: Path, bash: str, repo: Path) -> None:
    result = _run(bash, tmp_path, "records.sh", "facts", "101", workdir=_posix_path(bash, repo))
    facts = dict(line.split("=", 1) for line in result.stdout.splitlines() if not line.startswith("dirty="))
    assert facts["slurm_cluster"] == "haic"
    assert facts["job_name"] == "train job"
    assert facts["branch"] == "main"
    assert facts["pushed"] == "no"
    assert len(facts["commit"]) == 40
    assert int(facts["submitted_ago"]) > 365 * 24 * 3600  # since the fake 2020 submit time
    assert "dirty=?? train.py" in result.stdout


def test_facts_list_at_most_100_uncommitted_files(tmp_path: Path, bash: str, repo: Path) -> None:
    for n in range(104):
        (repo / f"f{n:03}.txt").write_text("x\n", encoding="utf-8")
    result = _run(bash, tmp_path, "records.sh", "facts", "101", workdir=_posix_path(bash, repo))
    lines = result.stdout.splitlines()
    assert sum(line.startswith("dirty=") for line in lines) == 100
    assert "dirty_omitted=5" in lines  # 104 files and train.py


def test_facts_without_workdir_report_only_the_cluster(tmp_path: Path, bash: str) -> None:
    result = _run(bash, tmp_path, "records.sh", "facts", "101")
    assert result.returncode == 0
    assert "slurm_cluster=haic" in result.stdout
    assert "workdir=" not in result.stdout


_REFS_TOOLS = {
    "scontrol": _FAKE_SCONTROL,
    # squeue knows job 7 only; sacct knows job 8 only.
    "squeue": '#!/bin/sh\ncase "$*" in *" 7 "*) echo 7 ;; esac\n',
    "sacct": '#!/bin/sh\ncase "$*" in *" 8 "*) echo 8 ;; esac\n',
}


def test_refs_finds_jobs_and_commits_and_omits_the_rest(tmp_path: Path, bash: str) -> None:
    binp = tmp_path / "bin"
    binp.mkdir()
    for name, body in _REFS_TOOLS.items():
        (binp / name).write_text(body, encoding="utf-8", newline="\n")
        (binp / name).chmod(0o755)
    stored = tmp_path / "home" / ".ctun" / "jobs" / "haic"
    stored.mkdir(parents=True)
    (stored / "9.json").write_text("{}", encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short=7", "HEAD"], capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()

    env = dict(os.environ, PATH=f"{binp}{os.pathsep}{os.environ['PATH']}", HOME=str(tmp_path / "home"))
    refs = ["job:7", "job:8", "job:9", "job:10", f"commit:{commit}", "commit:deadbee"]
    result = subprocess.run(
        [bash, str(SCRIPTS / "records.sh"), "refs", _posix_path(bash, repo), "--", *refs],
        capture_output=True, text=True, encoding="utf-8", env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["job:7", "job:8", "job:9", f"commit:{commit}"]
