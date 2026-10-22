"""Exceptions raised by the Python API (:mod:`cluster_tunnel.api`).

Every failure is a :class:`ClusterTunnelError`. The ones the CLI reports with a
stable exit code carry it as ``exit_code`` (with the matching ``ctun-error:``
``marker``), so a program using the API can branch on exactly what an agent
driving ``ctun`` branches on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Sequence

from cluster_tunnel.constants import ERROR_MARKERS, ExitCode

if TYPE_CHECKING:
    from cluster_tunnel.budget import Decision


class ClusterTunnelError(Exception):
    """Base class of the errors the API raises.

    ``exit_code`` is the :class:`~cluster_tunnel.constants.ExitCode` ``ctun``
    exits with for this failure, or ``None`` where it exits with a plain 1.
    """

    exit_code: Optional[int] = None

    @property
    def marker(self) -> Optional[str]:
        """The ``ctun-error: <marker>`` word for this failure, if it has one."""
        return ERROR_MARKERS.get(self.exit_code) if self.exit_code is not None else None


class ConfigError(ClusterTunnelError):
    """The config file is missing or invalid."""


class UnknownClusterError(ClusterTunnelError):
    """The config has no cluster of that name."""


class LoginRequired(ClusterTunnelError):
    """There is no live tunnel to the cluster; a person has to log in first."""

    exit_code = ExitCode.LOGIN_REQUIRED

    def __init__(self, cluster: str):
        super().__init__(
            f"No live tunnel for '{cluster}'. Run `ctun -t {cluster} login` "
            f"(or `ctun -t {cluster} login --interactive`) first."
        )
        self.cluster = cluster


class BudgetBlocked(ClusterTunnelError):
    """The budget guard blocked a job submission; nothing was sent to the cluster.

    ``decision`` holds the guard's verdict: its ``reason`` and, when the usage
    could be measured, ``used``/``limit``/``unit``.
    """

    def __init__(self, cluster: str, command: Sequence[str], decision: "Decision"):
        super().__init__(f"Submission blocked on '{cluster}': {decision.reason}")
        self.cluster = cluster
        self.command = list(command)
        self.decision = decision


class BudgetExhausted(BudgetBlocked):
    """Blocked because the session's usage is at or over its limit."""

    exit_code = ExitCode.BUDGET_EXHAUSTED


class BudgetGuardError(BudgetBlocked):
    """Blocked because the usage could not be verified (``fail_mode: closed``)."""

    exit_code = ExitCode.BUDGET_GUARD_ERROR


class LoginError(ClusterTunnelError):
    """A login could not open the tunnel, or was cancelled.

    ``ssh_failed`` is true when ssh itself failed, where its own diagnostics
    (``verbose`` > 0) say why; with ``verbose`` they are in ``diagnostics``.
    After a login with a password, ``diagnostics`` also names a refused host
    key without ``verbose``: no retry with a password fixes that.
    """

    def __init__(
        self,
        message: str,
        cluster: str,
        *,
        ssh_failed: bool = False,
        diagnostics: Optional[str] = None,
    ):
        super().__init__(message)
        self.cluster = cluster
        self.ssh_failed = ssh_failed
        self.diagnostics = diagnostics


class NoteReferenceInvalid(ClusterTunnelError):
    """A job note names a ``job:<id>`` (or, in an edit, a ``commit:<hash>``) the cluster
    doesn't know; nothing ran, or the edit was not saved.

    ``unknown`` lists those references; it is empty when the check itself
    could not run, which refuses the note all the same.
    """

    exit_code = ExitCode.NOTE_REFERENCE_INVALID

    def __init__(self, message: str, unknown: Sequence[str] = ()):
        super().__init__(message)
        self.unknown = list(unknown)


class RecordError(ClusterTunnelError):
    """A job record could not be changed: the job is unknown, the cluster keeps no
    records, there is no note to confirm, or the record could not be read or stored."""


class TransportError(ClusterTunnelError):
    """The transport around ssh failed (on Windows: the WSL bridge)."""


class HostKeyError(ClusterTunnelError):
    """A host key could not be fetched or pinned, or isn't the one confirmed."""


def budget_blocked(cluster: str, command: Sequence[str], decision: "Decision") -> BudgetBlocked:
    """The error for a blocked ``decision``: exhausted when it carries a usage
    number at or over the limit, otherwise not verifiable."""
    if decision.used is not None and decision.limit is not None:
        return BudgetExhausted(cluster, command, decision)
    return BudgetGuardError(cluster, command, decision)
