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
