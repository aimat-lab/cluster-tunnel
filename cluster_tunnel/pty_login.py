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
import signal
import subprocess
import sys
import tempfile
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
    verbose: int = 0,
) -> tuple[bool, bytes]:
    """Run ``master_argv`` in a pty, answering its prompts; return (live, transcript).

    Only the unfinished last line of output can be a prompt, and only once the
    output pauses: prompts have no trailing newline and are followed by
    silence, while banners and status lines end in a newline. Each prompt is
    classified by its wording (:func:`classify_prompt`) and answered with the
    matching secret, at most once. A prompt asked again (the answer was
    rejected), or one for a secret we don't have, ends the attempt at once
    instead of waiting out ``timeout``. ``transcript`` is everything the master
    printed, plus ssh's ``-v`` log when ``verbose``, for diagnostics.
    """
    # Imported here, not at module level: `pty` needs termios, which Windows
    # lacks, and ctun reads this module's source on Windows.
    import pty
    import select

    log_path = None
    if verbose:
        # `ssh -v` would write its debug lines into the pty, where words such as
        # "password" in them read as prompts; send them to a log file instead.
        handle, log_path = tempfile.mkstemp(prefix="ctun-ssh-", suffix=".log")
        os.close(handle)
        master_argv = [master_argv[0], "-E", log_path, *master_argv[1:]]

    secrets = {"password": password, "otp": otp}
    sent = {"password": False, "otp": False}

    pid, fd = pty.fork()
    if pid == 0:  # child: become the ssh master, attached to the pty
        try:
            # ssh leads this pty's session. When `ssh -f` backgrounds itself, the
            # exiting parent hangs up the pty, and that SIGHUP kills the new
            # master if it lands before the master's setsid(). An ignored SIGHUP
            # survives exec, and ssh keeps it.
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
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
                kind = classify_prompt(buf.rsplit(b"\n", 1)[-1])
                if kind is None:
                    buf = buf[-256:]  # bound the buffer to the current prompt line
                    continue
                # A prompt is the last thing ssh prints before it waits for input.
                # If more output follows at once, this was the start of a longer
                # line (a banner, say) arriving in pieces: read on first.
                try:
                    if select.select([fd], [], [], 0.15)[0]:
                        continue
                except (OSError, ValueError):
                    break
                if sent[kind] or not secrets.get(kind):
                    break  # asked again, or for a secret we lack: this login can't succeed
                try:
                    os.write(fd, secrets[kind].encode() + b"\n")
                except OSError:
                    break
                sent[kind] = True
                buf = b""  # start fresh so the next prompt is classified alone
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    # brief grace for the backgrounded master to register on the socket
    live = is_live(check_argv)
    for _ in range(6):
        if live:
            break
        time.sleep(0.3)
        live = is_live(check_argv)
    if not live:
        try:
            os.kill(pid, signal.SIGTERM)  # don't leave ssh waiting at a prompt
        except OSError:
            pass
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass

    if log_path:
        try:
            with open(log_path, "rb") as fh:
                transcript += fh.read()
            os.unlink(log_path)  # the master keeps writing to it; that's fine unlinked
        except OSError:
            pass
    return live, transcript


def main(payload: str) -> int:
    """Entry point when streamed into WSL; returns 0 if the master came up.

    ``payload`` is base64-encoded JSON of :func:`login`'s arguments. It travels
    inside the program text on stdin, so the secrets never appear in argv or the
    environment. With ``verbose`` set, a failed login's transcript (including
    ssh's diagnostics) goes to stderr.
    """
    args = json.loads(base64.b64decode(payload))
    live, transcript = login(**args)
    if not live and args.get("verbose") and transcript:
        sys.stderr.buffer.write(transcript)
        sys.stderr.flush()
    return 0 if live else 1
