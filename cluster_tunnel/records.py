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
import hashlib
import json
import re
import socket
import subprocess
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from cluster_tunnel import constants, ssh
from cluster_tunnel.ssh import ConnSpec

if TYPE_CHECKING:
    from cluster_tunnel.facts import JobFacts

SCRIPT_PATH = Path(__file__).parent / "scripts" / "records.sh"

SCHEMA_VERSION = 1

#: Characters of records per ``update`` call: its SSH command line must stay under
#: Windows' 32767-character limit for wsl.exe, and Linux's 128 KiB per argument on the cluster.
_UPDATE_BATCH_CHARS = 24_000

#: A job's ID as records are keyed: ``51319``, or ``51319_4`` for one task of an array.
_JOB_ID = re.compile(r"\d+(?:_\d+)?")


def is_job_id(text: str) -> bool:
    """True if ``text`` is a Slurm job ID a record can be stored under."""
    return _JOB_ID.fullmatch(text) is not None


def job_id_of(raw: dict[str, Any]) -> Optional[str]:
    """The job ID a raw record names, or ``None`` when it names none a record could be stored under."""
    job_id = raw.get("job_id")
    if isinstance(job_id, (str, int)) and not isinstance(job_id, bool) and is_job_id(str(job_id)):
        return str(job_id)
    return None


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


def projects(records: list[JobRecord]) -> list[dict[str, Any]]:
    """Each project the ``records`` name, by name, with its number of jobs; records without one are left out.

    An array counts once, also when some of its tasks have records of their own.
    """
    jobs: dict[str, set[str]] = {}
    for record in records:
        found = keys(record.job_id)
        if found and record.project and isinstance(record.project, str):
            jobs.setdefault(record.project, set()).add(found[-1])
    return [{"project": project, "jobs": len(jobs[project])} for project in sorted(jobs)]


def project_name(name: str) -> str:
    """``name`` as a project is stored: ``/`` nests, so no ``/`` or blanks at either end
    of a level, and no empty level (`` archive//htfm/ `` is ``archive/htfm``)."""
    return "/".join(level.strip() for level in name.split("/") if level.strip())


def renamed_project(project: Optional[str], old: str, new: str) -> Optional[str]:
    """``project`` once ``old`` is renamed to ``new``, with the projects nested in ``old``; ``None`` if it is neither.

    When ``new`` lies inside ``old`` (``htfm`` to ``htfm/old``), ``new`` and the
    projects in it are left alone: an earlier run renamed them already, and the
    rename must be safe to run again.
    """
    if not isinstance(project, str):
        return None
    if new.startswith(f"{old}/") and (project == new or project.startswith(f"{new}/")):
        return None
    if project == old:
        return new
    if project and project.startswith(f"{old}/"):
        return new + project[len(old):]
    return None


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


#: The version of a job that has no record yet; see :class:`Stored`.
ABSENT = "absent"


@dataclass
class Stored:
    """A raw record as read from the cluster, keys this ctun doesn't know included,
    with its ``version``: an update made from it is refused if the record has
    changed since (another machine edited it meanwhile)."""

    record: dict[str, Any]
    version: str


@dataclass
class Updated:
    """What :func:`update_many` did, by job ID."""

    stored: list[str] = field(default_factory=list)
    #: Changed on the cluster since they were read, so not stored: read them again.
    changed: list[str] = field(default_factory=list)


class UpdateError(RuntimeError):
    """Storing records failed; ``stored`` names those stored before it did."""

    def __init__(self, message: str, stored: list[str]):
        super().__init__(message)
        self.stored = stored


def update(spec: ConnSpec, record: dict[str, Any], version: str) -> bool:
    """Store the raw ``record`` in place of the job's current one, or as its first.

    ``version`` is that of the record it was made from (:attr:`Stored.version`,
    or :data:`ABSENT` for the job's first). Returns ``False`` without storing
    when the record changed since; raises :class:`RuntimeError` when storing fails.
    """
    return bool(update_many(spec, [(record, version)]).stored)


def update_many(spec: ConnSpec, records: list[tuple[dict[str, Any], str]]) -> Updated:
    """Store each ``(record, version)`` like :func:`update`, many per SSH call.

    Raises :class:`UpdateError` at the first call that fails; the records stored
    up to then, in earlier calls or in that one, stay stored.
    """
    done = Updated()
    for batch in _update_batches(records):
        res = call(spec, ["update", *batch])
        for line in res.stdout.splitlines():
            outcome, _, job_id = line.partition(" ")
            if outcome == "stored":
                done.stored.append(job_id)
            elif outcome == "changed":
                done.changed.append(job_id)
        if res.returncode != 0:
            job_ids = ", ".join(job_id for job_id in batch[0::3] if job_id not in done.stored + done.changed)
            raise UpdateError(f"could not store the records of jobs {job_ids}: {ssh.failure(res)}", done.stored)
    return done


def _update_batches(records: list[tuple[dict[str, Any], str]]) -> list[list[str]]:
    """``[job_id, version, base64 record, ...]`` lists of at most ``_UPDATE_BATCH_CHARS`` each, or one record if it is longer."""
    batches: list[list[str]] = []
    batch: list[str] = []
    batch_chars = 0
    for record, version in records:
        document = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        triple = [str(record["job_id"]), version, base64.b64encode(document.encode("utf-8")).decode("ascii")]
        triple_chars = sum(len(part) + 1 for part in triple)
        if batch and batch_chars + triple_chars > _UPDATE_BATCH_CHARS:
            batches.append(batch)
            batch, batch_chars = [], 0
        batch += triple
        batch_chars += triple_chars
    if batch:
        batches.append(batch)
    return batches


def read(spec: ConnSpec, job_id: str) -> Stored:
    """The record of ``job_id`` as stored; an empty one at :data:`ABSENT` if the job has none."""
    found = read_many(spec, [job_id])
    return found[0] if found else Stored({}, ABSENT)


def read_many(spec: ConnSpec, job_ids: list[str]) -> list[Stored]:
    """The records of ``job_ids`` that exist, as stored."""
    wanted = set(job_ids)
    return [item for item in _read_stored(spec, ["list", 0, *job_ids]) if job_id_of(item.record) in wanted]


def list_recent(spec: ConnSpec, since_minutes: int, job_ids: list[str]) -> list[JobRecord]:
    """Records written in the last ``since_minutes`` minutes, plus those of ``job_ids``."""
    return _read(spec, ["list", since_minutes, *job_ids])


def list_all(spec: ConnSpec) -> list[JobRecord]:
    """Every record on the cluster."""
    return _read(spec, ["all"])


def list_all_stored(spec: ConnSpec) -> list[Stored]:
    """Every record on the cluster as stored."""
    return _read_stored(spec, ["all"])


def _read(spec: ConnSpec, args: list[object]) -> list[JobRecord]:
    records = []
    for item in _read_stored(spec, args):
        try:
            records.append(JobRecord.from_dict(item.record))
        except (TypeError, KeyError):
            continue  # a damaged file must not hide the others
    return records


def _read_stored(spec: ConnSpec, args: list[object]) -> list[Stored]:
    res = call(spec, args)
    if res.returncode != 0:
        raise RuntimeError(f"could not read the job records: {ssh.failure(res)}")
    found = []
    # One record per line; not splitlines(), which also splits at characters a
    # JSON string may hold as they are, such as U+2028.
    for line in res.stdout.split("\n"):
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except ValueError:
            continue  # a damaged file must not hide the others
        if isinstance(document, dict):
            found.append(Stored(document, hashlib.sha256(line.encode("utf-8")).hexdigest()))
    return found
