"""Configuration loading and validation for ctun."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cluster_tunnel import constants, paths

PACKAGE_DIR = Path(__file__).parent


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    control_persist: str = constants.DEFAULT_CONTROL_PERSIST
    server_alive_interval: int = constants.DEFAULT_SERVER_ALIVE_INTERVAL
    server_alive_count_max: int = constants.DEFAULT_SERVER_ALIVE_COUNT_MAX
    socket_dir: Optional[str] = None  # filled by the loader if left blank
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
    requires_otp: bool = False
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
    raw = yaml.safe_load(path.read_text()) or {}
    config = Config(**raw)
    if not config.defaults.socket_dir:
        config.defaults.socket_dir = str(paths.socket_dir())
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


def setup_if_necessary(cli_path: str | None = None) -> Path:
    """Create the config file (+ budget dir + example script) if it's missing."""
    path = resolve_config_path(cli_path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(PACKAGE_DIR / "config.example.yaml", path)
        bdir = path.parent / "budget"
        bdir.mkdir(parents=True, exist_ok=True)
        template = PACKAGE_DIR / "budget_templates" / "horeka.sh"
        if template.exists():
            shutil.copy(template, bdir / "horeka.sh.example")
    return path
