"""The bundled budget templates run under any awk (mawk-safe: no gawk mktime()).

Each template is executed for real with a fake ``sacct`` on PATH that prints two
fully-bounded 1-hour jobs, so the expected job-hours are deterministic (2.000)
regardless of the wall clock or timezone. The mawk variant proves the fix: the
old ``awk mktime()`` form failed under mawk and (with fail_mode=closed) blocked
all submissions.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "cluster_tunnel" / "budget_templates"
SCRIPTS = sorted(TEMPLATES.glob("*.sh"))

# Two jobs, each exactly 3600s, both fully inside the session window -> 2.000 job-hours.
_FAKE_SACCT = (
    "#!/bin/sh\n"
    "cat <<'EOF'\n"
    "2020-01-01T00:00:00|2020-01-01T01:00:00|COMPLETED\n"
    "2020-06-01T12:00:00|2020-06-01T13:00:00|COMPLETED\n"
    "EOF\n"
)
_START_EPOCH = "1500000000"  # 2017 — before the canned jobs, well after the 2001 floor.


def _run(script: Path, tmp_path: Path, awk_bin: str | None) -> str:
    binp = tmp_path / "bin"
    binp.mkdir(exist_ok=True)
    fake = binp / "sacct"
    fake.write_text(_FAKE_SACCT)
    fake.chmod(0o755)
    if awk_bin:  # force the script's `awk` to a specific implementation
        (binp / "awk").symlink_to(awk_bin)
    env = dict(os.environ, PATH=f"{binp}{os.pathsep}{os.environ['PATH']}")
    r = subprocess.run(
        ["bash", str(script), _START_EPOCH, "cluster", "user"],
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_template_no_gawk_mktime(script: Path) -> None:
    # Ignore comment lines (the header explains *why* mktime is avoided).
    code = "\n".join(
        ln for ln in script.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    assert "mktime(" not in code


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_template_computes_job_hours(script: Path, tmp_path: Path) -> None:
    assert _run(script, tmp_path, None) == "2.000"


@pytest.mark.skipif(shutil.which("mawk") is None, reason="mawk not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_template_runs_under_mawk(script: Path, tmp_path: Path) -> None:
    assert _run(script, tmp_path, shutil.which("mawk")) == "2.000"
