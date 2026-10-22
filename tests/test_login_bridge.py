"""`login -i` with a front end behind CTUN_LOGIN_BRIDGE: it logs in, the popup stays shut."""

from __future__ import annotations

import json
import socket
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from cluster_tunnel import login_bridge
from cluster_tunnel.cli import cli
from tests.test_cli import _cfg, _norm


def _serve_once(address: Path, reply: bytes) -> list:
    """Answer one request on a Unix socket with `reply`; return the list the request lands in."""
    requests = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(address))
    server.listen(1)

    def answer() -> None:
        conn, _ = server.accept()
        with conn, conn.makefile("rwb") as pipe:
            requests.append(json.loads(pipe.readline()))
            pipe.write(reply)
        server.close()

    threading.Thread(target=answer, daemon=True).start()
    return requests


unix_only = pytest.mark.skipif(sys.platform == "win32", reason="the test server is a Unix socket")


@unix_only
def test_sends_the_cluster_and_limit_and_returns_the_outcome(tmp_path: Path, monkeypatch) -> None:
    address = tmp_path / "bridge.sock"
    requests = _serve_once(address, b'{"status": "live"}\n')
    monkeypatch.setenv(login_bridge.BRIDGE_VARIABLE, str(address))
    outcome = login_bridge.log_in("haicore", 20.0, config="/c.yaml", timeout=5)
    assert outcome.status == "live"
    assert requests == [{"cluster": "haicore", "session_limit": 20.0, "config": "/c.yaml", "timeout": 5}]


@unix_only
def test_a_window_that_closes_without_reply_declines(tmp_path: Path, monkeypatch) -> None:
    address = tmp_path / "bridge.sock"
    _serve_once(address, b"")
    monkeypatch.setenv(login_bridge.BRIDGE_VARIABLE, str(address))
    with pytest.raises(login_bridge.Unavailable):
        login_bridge.log_in("haicore", None, config="/c.yaml", timeout=5)


@unix_only
@pytest.mark.parametrize(
    "reply", [b'"live"\n', b"[]\n", b"null\n", b'{"status": "declined"}\n', b"{}\n", b"not json\n"]
)
def test_any_other_reply_declines_so_the_popup_opens(tmp_path: Path, monkeypatch, reply: bytes) -> None:
    address = tmp_path / "bridge.sock"
    _serve_once(address, reply)
    monkeypatch.setenv(login_bridge.BRIDGE_VARIABLE, str(address))
    with pytest.raises(login_bridge.Unavailable):
        login_bridge.log_in("haicore", None, config="/c.yaml", timeout=5)


@unix_only
def test_a_front_end_that_never_answers_times_out(tmp_path: Path, monkeypatch) -> None:
    address = tmp_path / "bridge.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(address))
    server.listen(1)  # takes the connection, never answers
    monkeypatch.setenv(login_bridge.BRIDGE_VARIABLE, str(address))
    try:
        with pytest.raises(login_bridge.NoAnswer):
            login_bridge.log_in("haicore", None, config="/c.yaml", timeout=0.2)
    finally:
        server.close()


@unix_only
def test_the_socket_is_closed_on_every_path(tmp_path: Path, monkeypatch) -> None:
    import gc
    import warnings

    address = tmp_path / "bridge.sock"
    _serve_once(address, b'{"status": "live"}\n')
    monkeypatch.setenv(login_bridge.BRIDGE_VARIABLE, str(address))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        login_bridge.log_in("haicore", None, config="/c.yaml", timeout=5)
        monkeypatch.setenv(login_bridge.BRIDGE_VARIABLE, str(tmp_path / "gone.sock"))
        with pytest.raises(login_bridge.Unavailable):
            login_bridge.log_in("haicore", None, config="/c.yaml", timeout=5)
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_without_a_window_it_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv(login_bridge.BRIDGE_VARIABLE, raising=False)
    with pytest.raises(login_bridge.Unavailable):
        login_bridge.log_in("haicore", None, config="/c.yaml", timeout=5)


def test_a_window_that_has_closed_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    gone = r"\\.\pipe\ctun-vscode-gone" if sys.platform == "win32" else str(tmp_path / "gone.sock")
    monkeypatch.setenv(login_bridge.BRIDGE_VARIABLE, gone)
    with pytest.raises(login_bridge.Unavailable):
        login_bridge.log_in("haicore", None, config="/c.yaml", timeout=5)


def _patch_login(tmp_path: Path, monkeypatch, outcome, *, live_after: bool = True) -> list:
    """The tunnel is down, and the front end answers ``outcome``; return the requests it got."""
    from cluster_tunnel import paths, popup, ssh

    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache")
    answers = iter([False, live_after])  # before the login, after it
    monkeypatch.setattr(ssh, "is_live", lambda spec: next(answers, live_after))
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a, **k: (_ for _ in ()).throw(AssertionError("popup")))
    requests = []

    def fake_log_in(name, limit, *, config, timeout):
        requests.append((name, limit, config, timeout))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(login_bridge, "log_in", fake_log_in)
    monkeypatch.setenv(login_bridge.BRIDGE_VARIABLE, "the-window")
    return requests


def test_login_keeps_the_session_the_front_end_started(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel import session

    config = _cfg(tmp_path, monkeypatch)
    requests = _patch_login(tmp_path, monkeypatch, login_bridge.Outcome("live"))
    started = session.start("localhost", limit=20.0, unit="jobh")  # by the front end's own login
    r = CliRunner().invoke(cli, ["-t", "localhost", "login", "-i", "--timeout", "30"])
    assert r.exit_code == 0, r.output
    assert "through the login bridge" in r.output
    assert session.load("localhost") == started
    assert requests == [("localhost", None, str(config), 30)]


def test_api_login_goes_through_the_bridge_too(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel import api

    _cfg(tmp_path, monkeypatch)
    _patch_login(tmp_path, monkeypatch, login_bridge.Outcome("live"))
    result = api.login("localhost", interactive=True, limit=5)
    assert result.bridged and result.interactive and not result.already_live and not result.session_kept
    assert result.session["limit"] == 5  # the front end kept no session here, so ctun starts one


def test_a_live_reply_without_a_tunnel_fails(tmp_path: Path, monkeypatch) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_login(tmp_path, monkeypatch, login_bridge.Outcome("live"), live_after=False)
    r = CliRunner().invoke(cli, ["-t", "localhost", "login", "-i"])
    assert r.exit_code == 1 and "reported a live tunnel to 'localhost', but there is none" in _norm(r)


@pytest.mark.parametrize(
    "outcome, said",
    [
        (login_bridge.Outcome("failed", "wrong OTP"), "through the login bridge failed: wrong OTP"),
        (login_bridge.Outcome("failed"), "through the login bridge failed."),
        (login_bridge.Outcome("cancelled"), "Login cancelled."),
        (login_bridge.NoAnswer(), "No answer through the login bridge within 120s."),
    ],
)
def test_login_reports_what_went_wrong_in_the_front_end(tmp_path: Path, monkeypatch, outcome, said: str) -> None:
    _cfg(tmp_path, monkeypatch)
    _patch_login(tmp_path, monkeypatch, outcome)
    r = CliRunner().invoke(cli, ["-t", "localhost", "login", "-i"])
    assert r.exit_code == 1 and said in _norm(r)


def test_a_declining_front_end_leaves_the_login_to_the_popup(tmp_path: Path, monkeypatch) -> None:
    from cluster_tunnel import popup

    _cfg(tmp_path, monkeypatch)
    _patch_login(tmp_path, monkeypatch, login_bridge.Unavailable())
    monkeypatch.setattr(popup, "gui_available", lambda: True)
    monkeypatch.setattr(popup, "prompt_credentials", lambda *a, **k: None)  # the human closes the popup
    r = CliRunner().invoke(cli, ["-t", "localhost", "login", "-i"])
    assert r.exit_code == 1 and "Login cancelled." in _norm(r)  # from the popup, not the bridge
