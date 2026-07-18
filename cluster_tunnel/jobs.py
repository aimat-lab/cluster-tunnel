"""Job overview: run the bundled ``jobs.sh`` probe over the tunnel and parse it.

``ctun jobs`` ships :data:`SCRIPT_PATH` to a cluster's login node via
``bash -s`` (see :func:`cluster_tunnel.ssh.feed_script`) and reads back one
pipe-delimited line per job. This module owns the wire format — the field order,
the split, and the active/finished ordering — so the CLI layer only renders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from cluster_tunnel import ssh
from cluster_tunnel.ssh import ConnSpec

PACKAGE_DIR = Path(__file__).parent

#: The bundled probe shipped over the tunnel; universal across Slurm clusters.
SCRIPT_PATH = PACKAGE_DIR / "scripts" / "jobs.sh"

#: Number of ``|``-separated columns each data line carries. The name is last,
#: so it is captured with ``split('|', _N_FIELDS - 1)`` and may itself contain
#: a stray ``|`` without shifting the earlier columns.
_N_FIELDS = 8


@dataclass
class Job:
    """One Slurm job as reported by the probe."""

    source: str  # "active" (live queue) or "done" (recently finished)
    jobid: str
    state: str
    elapsed: str
    timelimit: str
    partition: str
    nodes: str
    name: str

    @property
    def active(self) -> bool:
        return self.source == "active"

    def as_dict(self) -> dict:
        return asdict(self)


def parse_line(line: str) -> Job | None:
    """Parse one probe output line into a :class:`Job`, or ``None`` if malformed.

    The name field is last and split-limited, so job names containing ``|`` stay
    intact. Lines with too few fields (unexpected noise on stdout) are dropped.
    """
    line = line.rstrip("\n")
    if not line.strip():
        return None
    parts = line.split("|", _N_FIELDS - 1)
    if len(parts) != _N_FIELDS:
        return None
    source, jobid, state, elapsed, timelimit, partition, nodes, name = parts
    if source not in ("active", "done") or not jobid:
        return None
    return Job(source, jobid, state, elapsed, timelimit, partition, nodes, name)


def _order(jobs: list[Job]) -> list[Job]:
    """Active jobs first (in queue order), then finished jobs most-recent-first.

    The probe prints active lines in ``squeue`` order and finished lines in
    ``sacct`` chronological order, so reversing the finished group yields
    newest-finished-first. A job seen as both active and finished (it ended
    between the two probe calls) is kept only in its active form.
    """
    seen: set[str] = set()
    active: list[Job] = []
    done: list[Job] = []
    for job in jobs:
        if job.active:
            seen.add(job.jobid)
            active.append(job)
    for job in jobs:
        if not job.active and job.jobid not in seen:
            seen.add(job.jobid)
            done.append(job)
    done.reverse()
    return active + done


def query(spec: ConnSpec, user: str | None, since_minutes: int) -> list[Job]:
    """Run the probe on ``spec``'s login node and return its jobs, ordered.

    ``user`` may be ``None`` (or empty) to let the script resolve it remotely via
    ``id -un``. ``since_minutes`` is the finished-job history window; ``0`` skips
    accounting and returns only the live queue. Raises :class:`RuntimeError` if
    the probe cannot run (e.g. no ``squeue`` on the host).
    """
    res = ssh.feed_script(spec, SCRIPT_PATH, [user or "", since_minutes])
    if res.returncode != 0:
        detail = (res.stderr or "").strip() or f"probe exited {res.returncode}"
        raise RuntimeError(detail)
    jobs = [job for line in res.stdout.splitlines() if (job := parse_line(line))]
    return _order(jobs)
