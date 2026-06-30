"""Typed CLI errors carrying a stable exit code and a machine-readable marker.

``ctun run`` distinguishes its own preflight failures (no tunnel, budget
exhausted, budget probe failed) from a remote command's exit code. Each ctun
failure exits with a documented :class:`~cluster_tunnel.constants.ExitCode` and
prints a ``ctun-error: <marker>`` line to stderr so callers — especially coding
agents — can branch on the failure programmatically.
"""

from __future__ import annotations

from typing import NoReturn

import rich_click as click

from cluster_tunnel.constants import ERROR_MARKERS


def emit_marker(exit_code: int) -> None:
    """Print the ``ctun-error: <marker>`` stderr line for `exit_code`, if any."""
    marker = ERROR_MARKERS.get(exit_code)
    if marker:
        click.echo(f"ctun-error: {marker}", err=True)


class CtunError(click.ClickException):
    """A ctun preflight failure with a stable exit code.

    Subclasses ``ClickException`` so the human-facing message still renders the
    usual way; the only addition is a caller-supplied ``exit_code``. The stderr
    marker is emitted by :func:`fail` rather than in ``show()`` because
    rich_click renders exceptions through its own formatter and never calls it.
    """

    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = int(exit_code)


def fail(message: str, exit_code: int) -> NoReturn:
    """Emit the machine-readable marker, then raise a coded :class:`CtunError`."""
    emit_marker(exit_code)
    raise CtunError(message, exit_code)
