"""rsync transfer backend: argv construction (no real transfer)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cluster_tunnel import transfer
from cluster_tunnel.ssh import ConnSpec


def _spec(identity: str | None = None) -> ConnSpec:
    return ConnSpec("k", "user@host", Path("/sock/k"), "12h", 60, 3, identity)


def test_upload_argv_basic() -> None:
    argv = transfer.RsyncBackend().argv(
        _spec(), direction="upload", src="./a", dest="data", dry_run=False, extra=[]
    )
    assert argv[0] == "rsync"
    assert "-r" in argv and "-n" not in argv
    # rsync SRC DEST order: local src, then remote dest prefixed with the target.
    assert argv[-2:] == ["./a", "user@host:data"]
    # -e carries the control socket + non-master + batch options.
    e = argv[argv.index("-e") + 1]
    assert e.startswith("ssh ")
    assert "ControlPath=/sock/k" in e
    assert "ControlMaster=no" in e
    assert "BatchMode=yes" in e


def test_download_argv_reverses_direction() -> None:
    argv = transfer.RsyncBackend().argv(
        _spec(), direction="download", src="results", dest="./out", dry_run=False, extra=[]
    )
    # download: remote src first, local dest last.
    assert argv[-2:] == ["user@host:results", "./out"]


def test_dry_run_adds_n() -> None:
    argv = transfer.RsyncBackend().argv(
        _spec(), direction="upload", src="a", dest="b", dry_run=True, extra=[]
    )
    assert "-n" in argv


def test_identity_file_in_e_string() -> None:
    argv = transfer.RsyncBackend().argv(
        _spec(identity="~/.ssh/id"), direction="upload", src="a", dest="b",
        dry_run=False, extra=[],
    )
    e = argv[argv.index("-e") + 1]
    assert "-i" in e and "id" in e


def test_extra_args_passed_through_before_paths() -> None:
    argv = transfer.RsyncBackend().argv(
        _spec(), direction="upload", src="a", dest="b", dry_run=False,
        extra=["--exclude=*.tmp", "-z"],
    )
    assert "--exclude=*.tmp" in argv and "-z" in argv
    # extra flags precede the SRC/DEST positionals.
    assert argv.index("-z") < argv.index("a")


def test_get_backend_default_and_unknown() -> None:
    assert transfer.get_backend().name == "rsync"
    assert transfer.get_backend("rsync").name == "rsync"
    with pytest.raises(ValueError):
        transfer.get_backend("nope")
