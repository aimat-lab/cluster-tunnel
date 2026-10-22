"""References in a job note: ``commit:<hash>`` and ``job:<id>``, checked on the cluster.

A note may point at commits and jobs only through these explicit markers, so
the reader (and the VS Code extension) can link them, and ctun can tell when a
note points at something that does not exist.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

from cluster_tunnel import records, ssh
from cluster_tunnel.ssh import ConnSpec

_REFERENCE = re.compile(r"\b(?:commit:[0-9a-fA-F]{7,40}|job:\d+(?:_\d+)?)\b")


def references(note: str, kind: Optional[str] = None) -> list[str]:
    """Every distinct ``commit:<hash>`` / ``job:<id>`` marker in ``note``, in order.

    ``kind`` (``"commit"`` or ``"job"``) keeps only the markers of that kind.
    """
    found = dict.fromkeys(_REFERENCE.findall(note))
    return [ref for ref in found if kind is None or ref.startswith(f"{kind}:")]


def unknown_references(
    spec: ConnSpec, refs: list[str], *, project: Optional[str] = None, repos: Sequence[str] = ()
) -> list[str]:
    """The references in ``refs`` that the cluster does not know.

    Commits are looked up in ``repos`` and in the repos (working directories)
    of the records of ``project``, or of every record when no project is given.
    Raises :class:`RuntimeError` when the check itself cannot run.
    """
    if not refs:
        return []
    found_in = list(repos)
    if any(ref.startswith("commit:") for ref in refs):
        found_in += _record_repos(spec, project)
    res = records.call(spec, ["refs", *dict.fromkeys(found_in), "--", *refs])
    if res.returncode != 0:
        raise RuntimeError(f"could not check the note's references: {ssh.failure(res)}")
    found = set(res.stdout.split())
    return [ref for ref in refs if ref not in found]


def _record_repos(spec: ConnSpec, project: Optional[str]) -> list[str]:
    workdirs = (
        record.code.get("workdir")
        for record in records.list_all(spec)
        if project is None or record.project == project
    )
    return [workdir for workdir in workdirs if workdir]
