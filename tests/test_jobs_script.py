"""The bundled jobs.sh probe runs for real against fake squeue/sacct on PATH.

The fakes shadow the host's real Slurm tools (this machine may itself be a
controller). The finished-window filter is exercised deterministically by
emitting sacct End timestamps *relative to now* (via `date`), so a 60-minute
window keeps a 10-minute-old job and drops a 100-minute-old one regardless of
the wall clock or timezone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "cluster_tunnel" / "scripts" / "jobs.sh"

# Fake squeue: ignores its args and prints two already-formatted queue lines
# (squeue itself applies the -o template, so the fake emits the final text). One
# name carries a space to prove spaces survive.
_FAKE_SQUEUE = (
    "#!/bin/sh\n"
    "printf 'active|966737|RUNNING|14:44|7:00:00|booster|1|train job\\n'\n"
    "printf 'active|966738|PENDING|0:00|7:00:00|booster|1|eval\\n'\n"
)

# Fake sacct: one job that ended 10 min ago (inside a 60-min window), one that
# ended 100 min ago (outside), and one still running (End=Unknown -> skipped).
_FAKE_SACCT = (
    "#!/bin/sh\n"
    'inside=$(date -d "-10 minutes" +%Y-%m-%dT%H:%M:%S)\n'
    'outside=$(date -d "-100 minutes" +%Y-%m-%dT%H:%M:%S)\n'
    'printf "%s|1001|COMPLETED|00:10:00|01:00:00|small|1|inside_job\\n" "$inside"\n'
    'printf "%s|1002|COMPLETED|00:10:00|01:00:00|small|1|outside_job\\n" "$outside"\n'
    'printf "Unknown|1003|RUNNING|00:05:00|01:00:00|small|1|running_job\\n"\n'
)


def _bin(tmp_path: Path) -> Path:
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    for name, body in (("squeue", _FAKE_SQUEUE), ("sacct", _FAKE_SACCT)):
        f = binp / name
        f.write_text(body)
        f.chmod(0o755)
    return binp


def _run(tmp_path: Path, since_minutes: str) -> str:
    binp = _bin(tmp_path)
    env = dict(os.environ, PATH=f"{binp}{os.pathsep}{os.environ['PATH']}")
    r = subprocess.run(
        ["bash", str(SCRIPT), "someuser", since_minutes],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_active_jobs_always_listed(tmp_path: Path) -> None:
    out = _run(tmp_path, "60")
    assert "active|966737|RUNNING|14:44|7:00:00|booster|1|train job" in out
    assert "active|966738|PENDING|0:00|7:00:00|booster|1|eval" in out


def test_finished_window_filters_by_end(tmp_path: Path) -> None:
    out = _run(tmp_path, "60")
    assert "done|1001|COMPLETED" in out       # ended 10 min ago -> inside window
    assert "1002" not in out                  # ended 100 min ago -> outside window
    assert "1003" not in out                  # still running (End=Unknown) -> skipped


def test_since_zero_skips_accounting(tmp_path: Path) -> None:
    out = _run(tmp_path, "0")
    assert "active|966737" in out             # live queue still shown
    assert "done|" not in out                 # sacct never consulted


def test_squeue_missing_exits_nonzero(tmp_path: Path) -> None:
    # A PATH with coreutils but no squeue makes the probe refuse (exit 3) rather
    # than silently report an empty queue.
    binp = tmp_path / "onlycore"
    binp.mkdir()
    # Only expose the coreutils the script may touch (id, date), not squeue/sacct.
    for tool in ("id", "date"):
        src = shutil.which(tool)
        if src:
            (binp / tool).symlink_to(src)
    env = dict(os.environ, PATH=str(binp))
    bash = shutil.which("bash") or "/bin/bash"  # absolute: the restricted PATH lacks it
    r = subprocess.run(
        [bash, str(SCRIPT), "someuser", "60"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 3
    assert "squeue not found" in r.stderr
