"""Open the SSH master in a pseudo-terminal, typing the password and OTP.

Standard library only, and it must stay that way: on Windows, ctun streams this
file's source to ``python3 -I -`` inside WSL, where cluster_tunnel is not
installed, and appends a call to :func:`main`. On Linux and macOS,
:mod:`cluster_tunnel.popup` imports it and calls :func:`login` in-process.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time

# Cluster login asks for two distinct secrets — the service password and the
# one-time passcode (OTP) — in a sequence whose order varies by site. We classify
# each prompt by its wording and answer it with the matching secret. OTP keys are
# checked first because they are the more specific signal.
OTP_KEYS = (
    b"passcode",
    b"verification",
    b"one-time",
    b"otp",
    b"token",
    b"second factor",
    b"2fa",
)
PASSWORD_KEYS = (
    b"password",
    b"passphrase",
)
PROMPT_KEYS = OTP_KEYS + PASSWORD_KEYS


def looks_like_prompt(buf: bytes) -> bool:
    low = buf.lower()
    return any(key in low for key in PROMPT_KEYS)


def classify_prompt(buf: bytes) -> str | None:
    """Classify the most recent prompt as ``"otp"``, ``"password"``, or ``None``.

    OTP keywords are tested first: an OTP prompt rarely contains "password", but a
    banner or password prompt could mention a token, so the more specific signal
    wins.
    """
    low = buf.lower()
    if any(key in low for key in OTP_KEYS):
        return "otp"
    if any(key in low for key in PASSWORD_KEYS):
        return "password"
    return None


def is_live(check_argv: list[str]) -> bool:
    """True if the master answers ``check_argv`` (an ``ssh -O check`` command)."""
    try:
        res = subprocess.run(check_argv, stdin=subprocess.DEVNULL, capture_output=True)
    except OSError:
        return False
    return res.returncode == 0


def login(
    master_argv: list[str],
    check_argv: list[str],
    password: str,
    otp: str | None,
    timeout: float,
) -> tuple[bool, bytes]:
    """Run ``master_argv`` in a pty, answering its prompts; return (live, transcript).

    Each prompt is classified by its wording (:func:`classify_prompt`) and
    answered with the matching secret; each secret is sent at most once. A
    missing/blank ``otp`` simply means OTP prompts go unanswered (for clusters
    that do not use one). ``transcript`` is everything the master printed, for
    diagnostics when the login fails.
    """
    # Imported here, not at module level: `pty` needs termios, which Windows
    # lacks, and ctun reads this module's source on Windows.
    import pty
    import select

    secrets = {"password": password, "otp": otp}
    sent = {"password": False, "otp": False}

    pid, fd = pty.fork()
    if pid == 0:  # child: become the ssh master, attached to the pty
        try:
            os.execvp(master_argv[0], master_argv)
        except Exception:
            os._exit(127)

    deadline = time.time() + timeout
    buf = b""
    transcript = b""
    try:
        while time.time() < deadline:
            if is_live(check_argv):
                break
            try:
                rlist, _, _ = select.select([fd], [], [], 0.3)
            except (OSError, ValueError):
                break
            if fd in rlist:
                try:
                    data = os.read(fd, 1024)
                except OSError:
                    break
                if not data:  # child exited / EOF
                    break
                buf += data
                transcript += data
                kind = classify_prompt(buf)
                if kind and not sent[kind] and secrets.get(kind):
                    try:
                        os.write(fd, secrets[kind].encode() + b"\n")
                    except OSError:
                        break
                    sent[kind] = True
                    buf = b""  # start fresh so the next prompt is classified alone
                else:
                    buf = buf[-256:]  # bound the buffer to the current prompt line
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass

    # brief grace for the backgrounded master to register on the socket
    live = is_live(check_argv)
    for _ in range(6):
        if live:
            break
        time.sleep(0.3)
        live = is_live(check_argv)
    return live, transcript


def main(payload: str) -> int:
    """Entry point when streamed into WSL; returns 0 if the master came up.

    ``payload`` is base64-encoded JSON of :func:`login`'s arguments plus
    ``verbose``. It travels inside the program text on stdin, so the secrets
    never appear in argv or the environment. With ``verbose`` set, a failed
    login's transcript (ssh's diagnostics) goes to stderr.
    """
    args = json.loads(base64.b64decode(payload))
    verbose = args.pop("verbose", 0)
    live, transcript = login(**args)
    if not live and verbose and transcript:
        sys.stderr.buffer.write(transcript)
        sys.stderr.flush()
    return 0 if live else 1
