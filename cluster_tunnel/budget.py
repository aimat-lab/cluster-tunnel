"""Session-scoped compute-budget guard.

Before a job-submitting command, run the cluster's budget script on the login
node (over the tunnel), compare the compute used since the session started to the
session limit, and hard-block if at/over. Threshold only — no per-job forecasting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cluster_tunnel import config as config_mod
from cluster_tunnel import session, ssh
from cluster_tunnel.config import Cluster, Config
from cluster_tunnel.ssh import ConnSpec


@dataclass
class Decision:
    allowed: bool
    reason: str
    used: Optional[float] = None
    limit: Optional[float] = None
    unit: str = "units"


def first_token_name(tokens: list[str]) -> str:
    """The bare command name of the first token (basename, no path)."""
    return os.path.basename(tokens[0]) if tokens else ""


def is_guarded(tokens: list[str], guard_commands: list[str]) -> bool:
    """True if the command is a job submission subject to the budget guard."""
    return first_token_name(tokens) in set(guard_commands)


def remote_user(spec: ConnSpec, cluster: Cluster) -> str:
    """The cluster username: from config, else queried over the tunnel."""
    if cluster.user:
        return cluster.user
    res = ssh.capture(spec, ["id", "-un"])
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "could not determine remote user")
    return res.stdout.strip()


def used_since(spec: ConnSpec, script_path: Path, start_epoch: int, user: str) -> float:
    """Run the budget script remotely and parse the single number it prints."""
    res = ssh.probe(spec, script_path, start_epoch, user)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"probe exited {res.returncode}")
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("budget script produced no output")
    try:
        return float(lines[-1].split()[0])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"could not parse budget number from: {res.stdout!r}") from exc


def _failure(cluster: Cluster, msg: str, limit: Optional[float], unit: str) -> Decision:
    """Apply fail_mode when the probe can't produce a number."""
    fail_mode = cluster.budget.fail_mode if cluster.budget else "closed"
    if fail_mode == "open":
        return Decision(True, f"{msg} (fail-open: allowed)", None, limit, unit)
    return Decision(False, f"{msg} (fail-closed: blocked)", None, limit, unit)


def decide(
    config: Config, name: str, spec: ConnSpec, tokens: list[str], config_path: Path
) -> Decision:
    """Decide whether `tokens` may run on cluster `name` under the budget guard."""
    cluster = config_mod.get_cluster(config, name)
    budget = cluster.budget

    if budget is None or not is_guarded(tokens, budget.guard_commands):
        return Decision(True, "not a guarded command")

    sess = session.load(name)
    limit = sess.get("limit") if sess else None
    unit = (sess.get("unit") if sess else None) or budget.unit

    if sess is None:
        return _failure(cluster, "no active session", limit, unit)
    if limit is None:
        return Decision(True, "no session limit set", unit=unit)

    script = config_mod.budget_script_path(config_path, name, budget.script)
    if not script.exists():
        return _failure(cluster, f"budget script not found: {script}", limit, unit)

    try:
        user = remote_user(spec, cluster)
        used = used_since(spec, script, int(sess["start_epoch"]), user)
    except Exception as exc:  # noqa: BLE001
        return _failure(cluster, f"budget probe failed: {exc}", limit, unit)

    if used >= limit:
        return Decision(
            False, f"session budget exhausted: {used} >= {limit} {unit}", used, limit, unit
        )
    return Decision(True, f"within budget: {used} < {limit} {unit}", used, limit, unit)
