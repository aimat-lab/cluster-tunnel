"""Per-cluster session state (start time + budget limit), in the cache.

A session == the lifetime of one authentication. It is stamped at `login` and
replaced only by re-auth; there is no manual reset. State is internal-only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from cluster_tunnel import paths


def _session_file(name: str) -> Path:
    return paths.sessions_dir() / f"{name}.json"


def start(name: str, *, limit: Optional[float], unit: str) -> dict:
    """Stamp a new session (resets the usage window). Returns the session dict."""
    paths.sessions_dir().mkdir(parents=True, exist_ok=True)
    start_epoch = int(time.time())
    data = {
        "session_id": f"{name}-{start_epoch}",
        "start_epoch": start_epoch,
        "limit": limit,
        "unit": unit,
    }
    _session_file(name).write_text(json.dumps(data, indent=2))
    return data


def load(name: str) -> Optional[dict]:
    """Return the current session dict for a cluster, or None."""
    f = _session_file(name)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear(name: str) -> None:
    """Drop the session for a cluster (e.g. on logout)."""
    _session_file(name).unlink(missing_ok=True)
