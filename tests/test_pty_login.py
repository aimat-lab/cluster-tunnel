"""The stdlib-only login driver, run for real against a fake ssh master.

The ``fake_master`` fixture (conftest.py) behaves like ssh where the driver has
broken before (a banner naming the password, flushed early input, re-prompts,
``-E`` logs, ``-f`` backgrounding) and, on the right answers, creates a marker
that its ``-O check`` stand-in treats as a live control socket.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cluster_tunnel import pty_login

needs_pty = pytest.mark.skipif(sys.platform == "win32", reason="needs a pty (Linux/macOS)")


def test_classify_prompt() -> None:
    assert pty_login.classify_prompt(b"user@host's password: ") == "password"
    assert pty_login.classify_prompt(b"Enter passphrase for key: ") == "password"
    assert pty_login.classify_prompt(b"Verification code: ") == "otp"
    assert pty_login.classify_prompt(b"OTP: ") == "otp"
    assert pty_login.classify_prompt(b"(MFA) Enter your passcode: ") == "otp"
    assert pty_login.classify_prompt(b"Last login: yesterday") is None


def test_looks_like_prompt() -> None:
    assert pty_login.looks_like_prompt(b"user@host's password: ")
    assert pty_login.looks_like_prompt(b"Verification code:")
    assert not pty_login.looks_like_prompt(b"Last login: yesterday on tty1")


@needs_pty
def test_login_answers_password_then_otp(fake_master) -> None:
    # The banner is not answered, and the backgrounded master survives the
    # hangup (SIGHUP) of the session it leaves.
    master, check, marker = fake_master
    live, transcript = pty_login.login(master, check, "s3cret", "123456", timeout=20)
    assert live and marker.exists()
    assert b"Verification code:" in transcript


@needs_pty
def test_wrong_otp_fails_fast(fake_master) -> None:
    # The OTP is asked again: give up at once instead of waiting out the timeout.
    master, check, marker = fake_master
    start = time.monotonic()
    live, _ = pty_login.login(master, check, "s3cret", "000000", timeout=60)
    assert not live and not marker.exists()
    assert time.monotonic() - start < 15


@needs_pty
def test_verbose_logs_to_a_file_and_reports_it(fake_master) -> None:
    master, check, marker = fake_master
    live, _ = pty_login.login(master, check, "s3cret", "123456", timeout=20, verbose=1)
    assert live  # -v debug lines naming "password" stay out of the pty
    marker.unlink()
    live, transcript = pty_login.login(master, check, "s3cret", "000000", timeout=20, verbose=1)
    assert not live and b"debug1: Authentications" in transcript


@needs_pty
def test_main_runs_when_streamed_to_python_stdin(fake_master) -> None:
    # What Windows does inside WSL: the module's source plus a main() call with
    # a base64 payload, fed to `python -I -` on stdin.
    master, check, marker = fake_master
    payload = base64.b64encode(
        json.dumps(
            {"master_argv": master, "check_argv": check, "password": "s3cret",
             "otp": "123456", "timeout": 20, "verbose": 0}
        ).encode("utf-8")
    ).decode("ascii")
    source = Path(pty_login.__file__).read_text(encoding="utf-8")
    program = f"{source}\nraise SystemExit(main({payload!r}))\n"
    res = subprocess.run(
        [sys.executable, "-I", "-"], input=program.encode("utf-8"), capture_output=True
    )
    assert res.returncode == 0, res.stderr
    assert marker.exists()
