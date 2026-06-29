"""Package-wide constants shared by the CLI and core modules."""

from __future__ import annotations

import pathlib

#: Absolute path to the bundled VERSION file — the single source of truth for
#: the package version (kept in sync with pyproject.toml by `bump-my-version`).
VERSION_PATH = pathlib.Path(__file__).parent / "VERSION"


def get_version() -> str:
    """Return the package version, read from the bundled ``VERSION`` file."""
    return VERSION_PATH.read_text().strip()


#: appdirs identifiers for ~/.config/cluster-tunnel and ~/.cache/cluster-tunnel.
APP_NAME = "cluster-tunnel"
APP_AUTHOR = "jonas"

#: Commands whose first token marks a job submission subject to the budget guard.
DEFAULT_GUARD_COMMANDS = ["sbatch", "srun", "salloc"]

#: SSH ControlMaster defaults.
DEFAULT_CONTROL_PERSIST = "12h"
DEFAULT_SERVER_ALIVE_INTERVAL = 60
DEFAULT_SERVER_ALIVE_COUNT_MAX = 3

#: What to do when a budget probe errors: "closed" blocks, "open" allows.
DEFAULT_FAIL_MODE = "closed"

#: Order in which to try terminal emulators for `login --interactive`.
TERMINAL_CANDIDATES = ["gnome-terminal", "xfce4-terminal", "x-terminal-emulator", "xterm"]
