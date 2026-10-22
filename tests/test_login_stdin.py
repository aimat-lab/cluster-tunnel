"""`login --credentials-stdin`: the secrets come through a pipe, never a window or argv."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from cluster_tunnel.cli import cli
from tests.test_cli import _cfg, _norm


def _patch_login(tmp_path: Path, monkeypatch) -> list:
    from cluster_tunnel import paths, popup, ssh

    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")
    live = iter([False, True])  # down before the login, up after it
    monkeypatch.setattr(ssh, "is_live", lambda spec: next(live))
    logins = []

    def fake_login(spec, password, otp, timeout, verbose=0):
        logins.append((password, otp))
        return True, ""

    monkeypatch.setattr(popup, "open_with_password", fake_login)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a, **k: (_ for _ in ()).throw(AssertionError("popup")))
    return logins


def test_logs_in_with_piped_secrets_and_limit(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel import session

    _cfg(tmp_path, monkeypatch)
    logins = _patch_login(tmp_path, monkeypatch)
    r = CliRunner().invoke(
        cli, ["-t", "localhost", "login", "--credentials-stdin"],
        input='{"password": "s3cret", "otp": "123456", "limit": 20}',
    )
    assert r.exit_code == 0, r.output
    assert logins == [("s3cret", "123456")]
    assert session.load("localhost")["limit"] == 20.0
    assert "s3cret" not in r.output


def test_rejects_input_that_is_not_json(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    logins = _patch_login(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "localhost", "login", "--credentials-stdin"], input="s3cret")
    assert r.exit_code != 0
    assert "not JSON" in r.output
    assert logins == []


def test_reads_a_non_ascii_password_as_utf8(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    logins = _patch_login(tmp_path, monkeypatch)
    r = CliRunner().invoke(
        cli, ["-t", "localhost", "login", "--credentials-stdin"],
        input='{"password": "Grüße€"}'.encode("utf-8"),
    )
    assert r.exit_code == 0, r.output
    assert logins == [("Grüße€", None)]


@pytest.mark.parametrize(
    "payload",
    [
        '{"password": 0}',
        '{"password": "x", "otp": false}',
        '{"password": "x", "limit": true}',
        '{"password": "x", "limit": NaN}',
        '{"password": "x", "limit": "20"}',
        '["x"]',
    ],
)
def test_rejects_values_of_the_wrong_type(tmp_path: Path, monkeypatch, payload: str) -> None:
    _cfg(tmp_path, monkeypatch)
    logins = _patch_login(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["-t", "localhost", "login", "--credentials-stdin"], input=payload)
    assert r.exit_code == 1 and "--credentials-stdin:" in _norm(r)
    assert logins == []
