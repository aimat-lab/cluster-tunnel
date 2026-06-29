"""Session-scoped compute-budget guard.

Before a job-submitting command, run the cluster's budget script on the login
node (over the tunnel), compare the compute used since the session started to the
session limit, and hard-block if at/over. Threshold only — no per-job forecasting.
"""

from __future__ import annotations

import os
import re
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


_ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def first_command_token(tokens: list[str]) -> str:
    """The first real command token, skipping leading ``VAR=value`` env assignments.

    ``ctun run -- FOO=1 sbatch job.sh`` should still be recognised as an ``sbatch``
    submission, so leading shell-style environment assignments are ignored.
    """
    for tok in tokens:
        if _ENV_ASSIGN.fullmatch(tok):
            continue
        return tok
    return ""


def first_token_name(tokens: list[str]) -> str:
    """The bare command name of the first real token (basename, no path)."""
    return os.path.basename(first_command_token(tokens))


def is_guarded(tokens: list[str], guard_commands: list[str]) -> bool:
    """True if the command is a job submission subject to the budget guard.

    Each entry in ``guard_commands`` is treated as a **regex** matched (fullmatch)
    against the command's bare name, so robust patterns like ``aslurmx?`` or
    ``s(batch|run|alloc)`` work. An entry that isn't valid regex falls back to a
    literal comparison, so plain names like ``sbatch`` keep working. Matching is
    case-insensitive and ignores any path prefix or leading env assignments.
    """
    name = first_token_name(tokens)
    if not name:
        return False
    for pattern in guard_commands:
        try:
            if re.fullmatch(pattern, name, re.IGNORECASE):
                return True
        except re.error:
            if pattern == name:  # invalid regex -> literal match
                return True
    return False


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
    # A null or negative limit (e.g. -1) means "infinite" — no budget guard.
    if limit is None or limit < 0:
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
