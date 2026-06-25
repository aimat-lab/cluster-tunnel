"""Filesystem locations for ctun (config + cache dirs)."""

from __future__ import annotations

from pathlib import Path

from appdirs import user_cache_dir, user_config_dir

from cluster_tunnel import constants


def config_dir() -> Path:
    """~/.config/cluster-tunnel (XDG-aware, cross-platform)."""
    return Path(user_config_dir(constants.APP_NAME, constants.APP_AUTHOR))


def cache_dir() -> Path:
    """~/.cache/cluster-tunnel (XDG-aware, cross-platform)."""
    return Path(user_cache_dir(constants.APP_NAME, constants.APP_AUTHOR))


def socket_dir() -> Path:
    """Where SSH ControlMaster sockets live (default; overridable in config)."""
    return cache_dir() / "sockets"


def sessions_dir() -> Path:
    """Internal per-cluster session state (start time, limit)."""
    return cache_dir() / "sessions"


def budget_dir() -> Path:
    """Per-cluster budget scripts, alongside the config file."""
    return config_dir() / "budget"


def ensure_runtime_dirs() -> None:
    """Create the cache-backed runtime directories if they don't exist yet."""
    socket_dir().mkdir(parents=True, exist_ok=True)
    sessions_dir().mkdir(parents=True, exist_ok=True)
