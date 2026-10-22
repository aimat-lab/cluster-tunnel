"""Job records: one JSON file per job, stored on the cluster.

``ctun run`` writes a record for each ``Submitted batch job <N>`` in its output.
The record holds what the agent said about the job (``project``, ``note``) and
the facts ctun collected itself (:mod:`cluster_tunnel.facts`). ``ctun jobs
edit`` changes the project and the note later, and logs each change in the record; the
facts are never changed. For a job that has no record (e.g. one submitted
with plain ``sbatch``), ``ctun jobs edit`` creates one without facts. ``records.sh``
keeps them in ``~/.ctun/jobs/<ClusterName>/<job id>.json`` on the cluster, so
every machine that works there sees the same records. It also collects a job's
facts (:mod:`cluster_tunnel.facts`) and checks a note's references
(:mod:`cluster_tunnel.notes`) there; :func:`call` runs it.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import subprocess
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from cluster_tunnel import constants, ssh
from cluster_tunnel.ssh import ConnSpec

if TYPE_CHECKING:
    from cluster_tunnel.facts import JobFacts

SCRIPT_PATH = Path(__file__).parent / "scripts" / "records.sh"

SCHEMA_VERSION = 1

#: A job's ID as records are keyed: ``51319``, or ``51319_4`` for one task of an array.
_JOB_ID = re.compile(r"\d+(?:_\d+)?")


def is_job_id(text: str) -> bool:
    """True if ``text`` is a Slurm job ID a record can be stored under."""
    return _JOB_ID.fullmatch(text) is not None


def keys(job_id: str) -> list[str]:
    """The records a job as squeue lists it may have, the closest first.

    A task of an array (``123_4``, pending ``123_[5-9]``) or a component of a
    heterogeneous job (``123+0``) has its own record only if ``jobs edit`` gave
    it one; otherwise it shares the record ``run`` wrote for the job, ``123``.
    """
    base = re.match(r"\d+", job_id)
    found = [job_id] if is_job_id(job_id) else []
    if base and base.group() != job_id:
        found.append(base.group())
    return found


@dataclass
class JobRecord:
    """One job. Facts are ``None`` (or empty) in a record ``ctun jobs edit`` created without them."""

    schema_version: int
    slurm_cluster: Optional[str]
    job_id: str
    job_name: Optional[str]
    project: Optional[str]
    note: Optional[str]
    #: True once a human confirmed the current note; a changed note is unconfirmed again.
    note_confirmed: bool
    submitted_at: Optional[str]
    submitted_from: Optional[str]
    #: ``{workdir, commit, branch, pushed, uncommitted, uncommitted_omitted}``; each may be
    #: ``None`` (unknown). ``uncommitted`` lists up to 100 files, ``uncommitted_omitted``
    #: counts the rest.
    code: dict[str, Any]
    command: list[str]
    #: One entry per edit: ``{at, from, <field>: [old, new], ...}``, oldest first.
    changes: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "JobRecord":
        """Parse a record; see :meth:`from_dict`."""
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JobRecord":
        """A record from its JSON object; keys this ctun does not know are ignored, missing ones are ``None``."""
        known = {f.name for f in fields(cls)}
        values = {name: raw.get(name) for name in known}
        values["note_confirmed"] = bool(values["note_confirmed"])
        values["code"] = values["code"] or {}
        values["command"] = values["command"] or []
        values["changes"] = values["changes"] or []
        values["job_id"] = str(raw["job_id"])
        return cls(**values)


def build(
    job_id: str,
    facts: JobFacts,
    *,
    project: Optional[str],
    note: Optional[str],
    command: list[str],
) -> JobRecord:
    """A record for ``job_id`` from the agent's ``project``/``note`` and ctun's ``facts``."""
    return JobRecord(
        schema_version=SCHEMA_VERSION,
        slurm_cluster=facts.slurm_cluster,
        job_id=job_id,
        job_name=facts.job_name,
        project=project,
        note=note,
        note_confirmed=False,
        submitted_at=_now(),
        submitted_from=socket.gethostname(),
        code={
            "workdir": facts.workdir,
            "commit": facts.commit,
            "branch": facts.branch,
            "pushed": facts.pushed,
            "uncommitted": facts.uncommitted,
            "uncommitted_omitted": facts.uncommitted_omitted,
        },
        command=command,
        changes=[],
    )


def edited(
    current: Optional[dict[str, Any]],
    job_id: str,
    *,
    project: Optional[str],
    note: Optional[str],
    confirm_note: bool,
) -> Optional[dict[str, Any]]:
    """The raw record ``current`` (``None``: the job has none yet) after one edit.

    ``project`` or ``note`` of ``None`` keeps the value, ``""`` clears it. A new
    note is unconfirmed unless ``confirm_note``. The edit is logged in
    ``changes``; keys this ctun does not know are kept. Returns ``None`` when the
    edit changes nothing. Raises :class:`ValueError` when ``confirm_note`` finds
    no note to confirm.
    """
    record = dict(current) if current else {"schema_version": SCHEMA_VERSION, "job_id": job_id}
    record.setdefault("note_confirmed", False)  # records before ``ctun jobs edit`` lack it
    wanted: dict[str, Any] = {}
    if project is not None:
        wanted["project"] = project or None
    if note is not None and (note or None) != record.get("note"):
        wanted["note"] = note or None
        wanted["note_confirmed"] = False
    if confirm_note:
        if not wanted.get("note", record.get("note")):
            raise ValueError(f"job {job_id} has no note to confirm")
        wanted["note_confirmed"] = True

    change = {key: [record.get(key), value] for key, value in wanted.items() if record.get(key) != value}
    if not change:
        return None
    record.update(wanted)
    record["changes"] = [*record.get("changes", []), {"at": _now(), "from": socket.gethostname(), **change}]
    return record


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def call(spec: ConnSpec, args: list[object]) -> subprocess.CompletedProcess:
    """Run ``records.sh`` with ``args`` (a command and its arguments) on the cluster.

    Bounded like every probe: a hung ``scontrol``, ``sacct`` or file system fails
    the call (exit code 124) instead of hanging ``run`` or ``jobs``.
    """
    return ssh.feed_script(spec, SCRIPT_PATH, args, timeout=constants.PROBE_TIMEOUT)


def write(spec: ConnSpec, record: JobRecord) -> None:
    """Store ``record`` on the cluster; raises :class:`RuntimeError` if that fails."""
    payload = base64.b64encode(record.to_json().encode("utf-8")).decode("ascii")
    res = call(spec, ["write", record.job_id, payload])
    if res.returncode != 0:
        raise RuntimeError(f"could not write the record of job {record.job_id}: {ssh.failure(res)}")


def update(spec: ConnSpec, record: dict[str, Any]) -> None:
    """Store the raw ``record`` on the cluster in place of the job's current one, or as its first."""
    document = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    payload = base64.b64encode(document.encode("utf-8")).decode("ascii")
    res = call(spec, ["update", record["job_id"], payload])
    if res.returncode != 0:
        raise RuntimeError(f"could not store the record of job {record['job_id']}: {ssh.failure(res)}")


def read(spec: ConnSpec, job_id: str) -> Optional[dict[str, Any]]:
    """The raw record of ``job_id`` as stored, or ``None`` if the job has none."""
    found = [raw for raw in _read_raw(spec, ["list", 0, job_id]) if str(raw.get("job_id")) == job_id]
    return found[0] if found else None


def list_recent(spec: ConnSpec, since_minutes: int, job_ids: list[str]) -> list[JobRecord]:
    """Records written in the last ``since_minutes`` minutes, plus those of ``job_ids``."""
    return _read(spec, ["list", since_minutes, *job_ids])


def list_all(spec: ConnSpec) -> list[JobRecord]:
    """Every record on the cluster."""
    return _read(spec, ["all"])


def _read(spec: ConnSpec, args: list[object]) -> list[JobRecord]:
    records = []
    for raw in _read_raw(spec, args):
        try:
            records.append(JobRecord.from_dict(raw))
        except (TypeError, KeyError):
            continue  # a damaged file must not hide the others
    return records


def _read_raw(spec: ConnSpec, args: list[object]) -> list[dict[str, Any]]:
    res = call(spec, args)
    if res.returncode != 0:
        raise RuntimeError(f"could not read the job records: {ssh.failure(res)}")
    documents = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except ValueError:
            continue  # a damaged file must not hide the others
        if isinstance(document, dict):
            documents.append(document)
    return documents
