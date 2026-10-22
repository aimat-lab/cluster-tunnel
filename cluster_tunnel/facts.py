"""Facts about a just-submitted job, collected by ctun on the cluster (never by the agent).

``records.sh facts`` runs on the login node over the tunnel and reports the
job's Slurm cluster, name and working directory, and the state of the git repo
in that directory: what code the job will run, as of submit time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from cluster_tunnel import records, ssh
from cluster_tunnel.ssh import ConnSpec


@dataclass
class JobFacts:
    """What ``records.sh facts`` found; ``None`` means the fact is unknown."""

    slurm_cluster: Optional[str] = None
    job_name: Optional[str] = None
    #: Seconds since Slurm took the job, measured on the cluster.
    submitted_ago: Optional[int] = None
    workdir: Optional[str] = None
    commit: Optional[str] = None
    branch: Optional[str] = None
    pushed: Optional[bool] = None
    #: ``git status --porcelain`` lines, the first 100; ``None`` when there is no git repo.
    uncommitted: Optional[list[str]] = None
    #: How many more uncommitted files there are than ``uncommitted`` lists.
    uncommitted_omitted: Optional[int] = None
    warnings: list[str] = field(default_factory=list)


def parse(output: str) -> JobFacts:
    """Parse the ``key=value`` lines of ``records.sh facts``; unknown keys are ignored."""
    facts = JobFacts()
    dirty: list[str] = []
    omitted = 0
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "dirty":
            dirty.append(value)
        elif key == "dirty_omitted" and value.isdigit():
            omitted = int(value)
        elif key == "submitted_ago" and value.lstrip("-").isdigit():
            facts.submitted_ago = int(value)
        elif key == "pushed":
            facts.pushed = value == "yes"
        elif key in ("slurm_cluster", "job_name", "workdir", "commit", "branch"):
            setattr(facts, key, value)
    if facts.commit is not None:
        facts.uncommitted = dirty
        facts.uncommitted_omitted = omitted
    return facts


def collect(spec: ConnSpec, job_id: str) -> JobFacts:
    """Collect the facts of ``job_id``; a failure leaves every fact unknown, with a warning."""
    res = records.call(spec, ["facts", job_id])
    facts = parse(res.stdout or "")
    if res.returncode != 0:
        facts.warnings.append(f"could not collect the facts of job {job_id}: {ssh.failure(res)}")
    for name in ("slurm_cluster", "workdir", "commit"):
        if getattr(facts, name) is None and res.returncode == 0:
            facts.warnings.append(f"job {job_id}: {name} unknown")
    return facts
