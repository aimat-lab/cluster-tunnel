"""Configuration loading and validation for ctun."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PureWindowsPath
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cluster_tunnel import constants, paths, wsl

PACKAGE_DIR = Path(__file__).parent


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control_persist: str = constants.DEFAULT_CONTROL_PERSIST
    server_alive_interval: int = constants.DEFAULT_SERVER_ALIVE_INTERVAL
    server_alive_count_max: int = constants.DEFAULT_SERVER_ALIVE_COUNT_MAX
    socket_dir: Optional[str] = None  # filled by the loader if left blank
    ssh_config: Optional[str] = None  # alternative ssh config file (ssh -F)
    wsl_distro: Optional[str] = None  # Windows: WSL distro to run ssh in (default distro if unset)
    terminal: str = "auto"


class AgentCfg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preamble: str = ""


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script: Optional[str] = None  # default: budget/<cluster>.sh
    session_limit: Optional[float] = None
    unit: str = "units"
    guard_commands: list[str] = Field(
        default_factory=lambda: list(constants.DEFAULT_GUARD_COMMANDS)
    )
    fail_mode: str = constants.DEFAULT_FAIL_MODE

    @model_validator(mode="after")
    def _check(self) -> "Budget":
        if self.fail_mode not in ("closed", "open"):
            raise ValueError("budget.fail_mode must be 'closed' or 'open'")
        return self


class Cluster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: Optional[str] = None
    user: Optional[str] = None
    ssh_alias: Optional[str] = None
    identity_file: Optional[str] = None
    ssh_config: Optional[str] = None  # overrides defaults.ssh_config for this cluster
    requires_otp: bool = False
    requires_password: bool = True
    control_persist: Optional[str] = None
    server_alive_interval: Optional[int] = None
    server_alive_count_max: Optional[int] = None
    description: str = ""
    restrictions: dict[str, Any] = Field(default_factory=dict)
    budget: Optional[Budget] = None

    @model_validator(mode="after")
    def _check(self) -> "Cluster":
        if not self.ssh_alias and not self.host:
            raise ValueError("cluster must set either 'ssh_alias' or 'host'")
        return self


class WebUI(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: Defaults = Field(default_factory=Defaults)
    agent: AgentCfg = Field(default_factory=AgentCfg)
    clusters: dict[str, Cluster] = Field(default_factory=dict)
    webui: WebUI = Field(default_factory=WebUI)


def resolve_config_path(cli_path: str | None = None) -> Path:
    """Config path precedence: --config > $CTUN_CONFIG > appdirs default."""
    if cli_path:
        return Path(cli_path).expanduser()
    env = os.environ.get("CTUN_CONFIG")
    if env:
        return Path(env).expanduser()
    return paths.config_dir() / "config.yaml"


def load_config(cli_path: str | None = None) -> Config:
    """Load and validate the config; raises FileNotFoundError if missing."""
    path = resolve_config_path(cli_path)
    if not path.exists():
        raise FileNotFoundError(
            f"No config at {path}. Run `ctun config --init` to create one."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = Config(**raw)
    if not config.defaults.socket_dir:
        # On Windows the master runs inside WSL, so its socket must live there.
        config.defaults.socket_dir = (
            wsl.DEFAULT_SOCKET_DIR if wsl.enabled() else str(paths.socket_dir())
        )
    return config


def get_cluster(config: Config, name: str) -> Cluster:
    """Look up a cluster by name with a helpful error if unknown."""
    if name not in config.clusters:
        known = ", ".join(sorted(config.clusters)) or "(none configured)"
        raise KeyError(f"Unknown cluster '{name}'. Configured: {known}")
    return config.clusters[name]


def resolve_target(cluster: Cluster) -> str:
    """The ssh destination: an ssh_config alias, or user@host, or host."""
    if cluster.ssh_alias:
        return cluster.ssh_alias
    if cluster.user:
        return f"{cluster.user}@{cluster.host}"
    return cluster.host  # type: ignore[return-value]


def budget_script_path(config_path: Path, name: str, script: str | None) -> Path:
    """Resolve a cluster's budget script, relative to the config file's dir."""
    rel = script or f"budget/{name}.sh"
    p = Path(rel).expanduser()
    return p if p.is_absolute() else config_path.parent / p


def validation_warnings(config: "Config", config_path: Path) -> list[str]:
    """Advisory (non-structural) problems pydantic can't catch on its own.

    These don't make the config malformed, but each one silently breaks something
    at run time, so ``config --validate`` surfaces them:

    - a cluster's ``budget.script`` doesn't exist on disk (the probe can't be
      shipped over the tunnel, so the guard fails — and with ``fail_mode:
      closed`` that blocks every submission);
    - a ``guard_commands`` entry isn't a valid regex (the matcher falls back to
      a literal comparison, so the intended pattern never fires);
    - on Windows, a path that ssh reads inside WSL (``socket_dir``,
      ``identity_file``, ``ssh_config``) is written as a Windows path, or an
      ``ssh_config`` starts with ``~``, which ssh does not expand for ``-F``.

    Returns a list of human-readable warning strings; empty means all clear.
    """
    warnings: list[str] = []
    for name, cluster in config.clusters.items():
        budget = cluster.budget
        if budget is None:
            continue
        script = budget_script_path(config_path, name, budget.script)
        if not script.exists():
            effect = "blocks" if budget.fail_mode == "closed" else "allows"
            warnings.append(
                f"cluster '{name}': budget script not found: {script} — the budget "
                f"guard will fail, and fail_mode='{budget.fail_mode}' {effect} submissions."
            )
        for pattern in budget.guard_commands:
            try:
                re.compile(pattern)
            except re.error as exc:
                warnings.append(
                    f"cluster '{name}': guard_commands entry {pattern!r} is not a valid "
                    f"regex ({exc}); it will only match the literal command name."
                )
    if wsl.enabled():
        warnings += _wsl_path_warnings(config)
    return warnings


def _wsl_path_warnings(config: "Config") -> list[str]:
    """On Windows, flag config paths that ssh, running inside WSL, can't use."""
    entries = [
        ("defaults.socket_dir", config.defaults.socket_dir),
        ("defaults.ssh_config", config.defaults.ssh_config),
    ]
    for name, cluster in config.clusters.items():
        entries += [
            (f"cluster '{name}': identity_file", cluster.identity_file),
            (f"cluster '{name}': ssh_config", cluster.ssh_config),
        ]
    warnings: list[str] = []
    for label, value in entries:
        if not value:
            continue
        if PureWindowsPath(value).drive or "\\" in value:
            warnings.append(
                f"{label} is a Windows path ({value}); on Windows ctun's ssh runs inside "
                "WSL and needs a WSL path, e.g. ~/.ssh/id_ed25519 or "
                "/mnt/c/Users/<you>/.ssh/config."
            )
        elif label.endswith("ssh_config") and value.startswith("~"):
            warnings.append(
                f"{label} starts with '~', which ssh does not expand for -F; use an "
                "absolute WSL path, e.g. /home/<you>/.ssh/config."
            )
    return warnings


def setup_if_necessary(cli_path: str | None = None) -> Path:
    """Create the config file (+ budget dir + bundled budget scripts) if missing.

    Every ``*.sh`` template shipped in ``budget_templates/`` is copied into the
    user's ``budget/`` directory under its own name, so a cluster whose config
    references ``budget/<cluster>.sh`` works out of the box (e.g. the validated
    ``haicore.sh``). Existing files are never overwritten.
    """
    path = resolve_config_path(cli_path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PACKAGE_DIR / "config.example.yaml", path)
        bdir = path.parent / "budget"
        bdir.mkdir(parents=True, exist_ok=True)
        templates = PACKAGE_DIR / "budget_templates"
        if templates.is_dir():
            for tpl in sorted(templates.glob("*.sh")):
                dest = bdir / tpl.name
                if not dest.exists():
                    shutil.copy(tpl, dest)
    return path
