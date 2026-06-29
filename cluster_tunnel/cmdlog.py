"""Per-cluster command log (commands only, never their output), in the cache.

A lightweight, temporary record of the commands sent to a cluster during the
current tunnel/session. It lives in the cache and is wiped on `logout` (and on a
fresh `login`). `status` reads it to show activity. Only the command tokens and a
timestamp are stored — never stdin/stdout/stderr.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from cluster_tunnel import paths


def _log_file(name: str) -> Path:
    return paths.cmdlog_dir() / f"{name}.jsonl"


def record(name: str, command: list[str]) -> None:
    """Append one command (with a timestamp) to the cluster's log."""
    paths.cmdlog_dir().mkdir(parents=True, exist_ok=True)
    entry = {"epoch": int(time.time()), "command": list(command)}
    with _log_file(name).open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


def summary(name: str) -> dict:
    """Return ``{'count': int, 'last_epoch': Optional[int]}`` for a cluster."""
    f = _log_file(name)
    if not f.exists():
        return {"count": 0, "last_epoch": None}
    count = 0
    last_epoch: Optional[int] = None
    try:
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            count += 1
            try:
                last_epoch = json.loads(line).get("epoch", last_epoch)
            except json.JSONDecodeError:
                continue
    except OSError:
        return {"count": 0, "last_epoch": None}
    return {"count": count, "last_epoch": last_epoch}


def entries(name: str) -> list[dict]:
    """Return all logged commands (oldest first) as ``{'epoch', 'command'}``."""
    f = _log_file(name)
    if not f.exists():
        return []
    out: list[dict] = []
    try:
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def clear(name: str) -> None:
    """Delete the command log for a cluster (on logout / fresh login)."""
    _log_file(name).unlink(missing_ok=True)
