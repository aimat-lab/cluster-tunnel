"""Interactive-login helpers (the dialog runs as a subprocess; mocked here)."""

from __future__ import annotations

import subprocess

from cluster_tunnel import popup
from cluster_tunnel.popup import Credentials


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


def test_classify_prompt() -> None:
    assert popup._classify_prompt(b"user@host's password: ") == "password"
    assert popup._classify_prompt(b"Enter passphrase for key: ") == "password"
    assert popup._classify_prompt(b"Verification code: ") == "otp"
    assert popup._classify_prompt(b"OTP: ") == "otp"
    assert popup._classify_prompt(b"(MFA) Enter your passcode: ") == "otp"
    assert popup._classify_prompt(b"Last login: yesterday") is None


def test_prompt_cancel(monkeypatch) -> None:
    monkeypatch.setattr(popup, "_dialog_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(
        subprocess, "run", lambda args, **k: subprocess.CompletedProcess(args, 1, "", "")
    )
    assert popup.prompt_credentials("k", "u@h", None) is None


def test_prompt_no_working_python(monkeypatch) -> None:
    monkeypatch.setattr(popup, "_dialog_python", lambda: None)
    assert popup.prompt_credentials("k", "u@h", None) is None


def test_looks_like_prompt() -> None:
    assert popup._looks_like_prompt(b"user@host's password: ")
    assert popup._looks_like_prompt(b"Verification code:")
    assert not popup._looks_like_prompt(b"Last login: yesterday on tty1")
