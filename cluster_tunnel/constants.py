"""Package-wide constants shared by the CLI and core modules."""

from __future__ import annotations

import enum
import pathlib

#: Absolute path to the bundled VERSION file — the single source of truth for
#: the package version (kept in sync with pyproject.toml by `bump-my-version`).
VERSION_PATH = pathlib.Path(__file__).parent / "VERSION"


def get_version() -> str:
    """Return the package version, read from the bundled ``VERSION`` file."""
    return VERSION_PATH.read_text(encoding="utf-8").strip()


#: appdirs name for ~/.config/cluster-tunnel and ~/.cache/cluster-tunnel.
APP_NAME = "cluster-tunnel"

#: appdirs author that 0.5.0 and earlier passed, which Windows put in the path
#: (%LOCALAPPDATA%\jonas\cluster-tunnel). Only used to find those installs.
LEGACY_APP_AUTHOR = "jonas"

#: Commands whose first token marks a job submission subject to the budget guard.
DEFAULT_GUARD_COMMANDS = ["sbatch", "srun", "salloc"]

#: SSH ControlMaster defaults.
DEFAULT_CONTROL_PERSIST = "7d"
DEFAULT_SERVER_ALIVE_INTERVAL = 60
DEFAULT_SERVER_ALIVE_COUNT_MAX = 10

#: Seconds a probe over the tunnel (budget usage, job list, job records, remote user) may take;
#: a probe that takes longer fails like any other (budget: per ``fail_mode``).
PROBE_TIMEOUT = 120

#: What to do when a budget probe errors: "closed" blocks, "open" allows.
DEFAULT_FAIL_MODE = "closed"

#: Order in which to try terminal emulators for `login --interactive`.
TERMINAL_CANDIDATES = ["gnome-terminal", "xfce4-terminal", "x-terminal-emulator", "xterm"]


class ExitCode(enum.IntEnum):
    """Stable exit codes for ctun's own ``run`` preflight failures.

    These are distinct from the remote command's exit code (propagated as-is,
    0-255) so an agent can tell *why* ctun stopped without scraping stderr.
    """

    LOGIN_REQUIRED = 10      #: No live tunnel — the caller must `login` again.
    BUDGET_EXHAUSTED = 11    #: Guarded command blocked: usage at/over the limit.
    BUDGET_GUARD_ERROR = 12  #: Guarded command blocked: budget couldn't be verified.
    NOTE_REFERENCE_INVALID = 13  #: `--note` names a job (or commit, in an edit) the cluster doesn't know.


#: Machine-readable markers printed to stderr (``ctun-error: <marker>``) alongside
#: each ExitCode, so agents have an unambiguous signal even if a numeric exit code
#: happens to collide with a remote command's own code.
ERROR_MARKERS = {
    ExitCode.LOGIN_REQUIRED: "login_required",
    ExitCode.BUDGET_EXHAUSTED: "budget_exhausted",
    ExitCode.BUDGET_GUARD_ERROR: "budget_guard_error",
    ExitCode.NOTE_REFERENCE_INVALID: "note_reference_invalid",
}
