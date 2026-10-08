"""Interactive-login helpers (the dialog runs as a subprocess; mocked here)."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import PurePosixPath

import pytest

from cluster_tunnel import popup, pty_login, ssh, wsl
from cluster_tunnel.popup import Credentials
from cluster_tunnel.ssh import ConnSpec


def test_prompt_parses(monkeypatch) -> None:
    monkeypatch.setattr(popup, "_dialog_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **k: subprocess.CompletedProcess(
            args, 0, '{"password":"s3cret","otp":"123456","limit":250.0}', ""
        ),
    )
    creds = popup.prompt_credentials("k", "u@h", 100.0)
    assert isinstance(creds, Credentials)
    assert creds.password == "s3cret"
    assert creds.otp == "123456"
    assert creds.limit == 250.0


def test_prompt_parses_without_otp(monkeypatch) -> None:
    monkeypatch.setattr(popup, "_dialog_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **k: subprocess.CompletedProcess(args, 0, '{"password":"s3cret","limit":1.0}', ""),
    )
    creds = popup.prompt_credentials("k", "u@h", None)
    assert creds.password == "s3cret"
    assert creds.otp is None


def test_prompt_parses_blank_password(monkeypatch) -> None:
    # A cluster with no service password: the dialog may return an empty password,
    # which must be accepted (the driver simply never sends it).
    monkeypatch.setattr(popup, "_dialog_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **k: subprocess.CompletedProcess(
            args, 0, '{"password":"","otp":"123456","limit":null}', ""
        ),
    )
    creds = popup.prompt_credentials("k", "u@h", None)
    assert creds.password == ""
    assert creds.otp == "123456"


def test_prompt_forwards_auth_flags(monkeypatch) -> None:
    captured: dict = {}

    def fake_run(args, **k):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, '{"password":"p","limit":1.0}', "")

    monkeypatch.setattr(popup, "_dialog_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(subprocess, "run", fake_run)

    # The dialog args end with [..., requires_otp, requires_password].
    popup.prompt_credentials("k", "u@h", None, "units", requires_otp=False, requires_password=True)
    assert captured["args"][-2:] == ["0", "1"]

    popup.prompt_credentials("k", "u@h", None, "units", requires_otp=True, requires_password=False)
    assert captured["args"][-2:] == ["1", "0"]


def test_prompt_cancel(monkeypatch) -> None:
    monkeypatch.setattr(popup, "_dialog_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(
        subprocess, "run", lambda args, **k: subprocess.CompletedProcess(args, 1, "", "")
    )
    assert popup.prompt_credentials("k", "u@h", None) is None


def test_prompt_no_working_python(monkeypatch) -> None:
    monkeypatch.setattr(popup, "_dialog_python", lambda: None)
    assert popup.prompt_credentials("k", "u@h", None) is None


def test_imports_without_pty() -> None:
    # Windows has no `pty` (it needs termios). The dialog half of this module must
    # still import there, so pty may only be imported inside the login loop.
    code = "import sys; sys.modules['pty'] = None; import cluster_tunnel.popup"
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8"
    )
    assert r.returncode == 0, r.stderr


def test_gui_available_on_windows_needs_no_display(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(popup, "_dialog_python", lambda: sys.executable)
    assert popup.gui_available()


def test_gui_available_on_linux_needs_a_display(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(popup, "_dialog_python", lambda: sys.executable)
    assert not popup.gui_available()


def test_windows_dialog_uses_running_interpreter(monkeypatch) -> None:
    # No /usr/bin/python3 on Windows, and a PATH `python3` may be the Store stub.
    monkeypatch.setattr(sys, "platform", "win32")
    assert popup._candidate_pythons() == [sys.executable]


def _spec() -> ConnSpec:
    return ConnSpec("k", "u@h", PurePosixPath("~/sockets/k"), "12h", 60, 3, None, wsl_distro="Ubuntu")


def _stub_ssh(monkeypatch, master: list[str], check: list[str]) -> None:
    monkeypatch.setattr(ssh, "prepare_socket", lambda spec: None)
    monkeypatch.setattr(ssh, "open_master_argv", lambda spec, verbose=0: master)
    monkeypatch.setattr(ssh, "check_argv", lambda spec: check)


def test_login_runs_driver_in_process_off_windows(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(wsl, "enabled", lambda: False)
    _stub_ssh(monkeypatch, ["ssh", "-M"], ["ssh", "-O", "check"])
    monkeypatch.setattr(pty_login, "login", lambda *a: calls.append(a) or (True, b""))
    assert popup.login_with_password(_spec(), "pw", "123", timeout=5)
    assert calls == [(["ssh", "-M"], ["ssh", "-O", "check"], "pw", "123", 5)]


def test_windows_login_streams_driver_with_secrets_off_argv(monkeypatch) -> None:
    seen: dict = {}

    def fake_run(argv, distro=None, *, stdin=b"", timeout=None):
        seen.update(argv=argv, distro=distro, stdin=stdin)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(wsl, "enabled", lambda: True)
    monkeypatch.setattr(wsl, "run", fake_run)
    _stub_ssh(monkeypatch, ["ssh", "-M"], ["ssh", "-O", "check"])
    assert popup.login_with_password(_spec(), "s3cret", "123456", timeout=5)

    assert seen["argv"] == ["python3", "-I", "-"] and seen["distro"] == "Ubuntu"
    program = seen["stdin"].decode("utf-8")
    assert "s3cret" not in program  # only inside the base64 payload
    encoded = program.rsplit("main(", 1)[1].split(")", 1)[0].strip("'")
    args = json.loads(base64.b64decode(encoded))
    assert (args["password"], args["otp"], args["master_argv"]) == ("s3cret", "123456", ["ssh", "-M"])


@pytest.mark.skipif(sys.platform == "win32", reason="runs the pty driver locally")
def test_windows_login_program_works_end_to_end(monkeypatch, fake_master) -> None:
    # Run the exact program Windows streams into WSL, locally instead of
    # through wsl.exe, against the fake master.
    def local_run(argv, distro=None, *, stdin=b"", timeout=None):
        res = subprocess.run([sys.executable, *argv[1:]], input=stdin, capture_output=True, timeout=timeout)
        return subprocess.CompletedProcess(argv, res.returncode, "", res.stderr.decode())

    master, check, marker = fake_master
    monkeypatch.setattr(wsl, "enabled", lambda: True)
    monkeypatch.setattr(wsl, "run", local_run)
    _stub_ssh(monkeypatch, master, check)
    assert popup.login_with_password(_spec(), "s3cret", "123456", timeout=20)
    assert marker.exists()
