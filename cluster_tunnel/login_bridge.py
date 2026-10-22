"""Hand ``login --interactive`` to the front end that started the agent.

A front end (such as the ctun VS Code extension, in each of its windows) serves
a local pipe and names it in ``CTUN_LOGIN_BRIDGE``. Agents it starts inherit the
variable, so their ``ctun login -i`` asks the front end instead of popping up a
dialog. The front end asks the human and logs in with its own
``ctun login --credentials-stdin``; only the outcome comes back over the pipe, so
the secrets never reach the process the agent started.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from dataclasses import dataclass
from typing import Optional

BRIDGE_VARIABLE = "CTUN_LOGIN_BRIDGE"

#: The outcomes a front end may reply with; any other reply declines the login.
STATUSES = ("live", "cancelled", "failed")


class Unavailable(Exception):
    """No front end took the login; the caller falls back to the popup."""


class NoAnswer(Exception):
    """The front end took the login but gave no outcome in time."""


@dataclass
class Outcome:
    """``status`` is ``live``, ``cancelled`` or ``failed``; ``message`` explains a failure."""

    status: str
    message: str = ""


def log_in(cluster_name: str, default_limit: Optional[float], *, config: str, timeout: float) -> Outcome:
    """Ask the front end to log in to ``cluster_name`` and wait for its outcome.

    The request is one line of JSON, ``{"cluster", "session_limit", "config",
    "timeout"}``: the front end logs in with
    ``ctun --config <config> -t <cluster> login --credentials-stdin``, so it uses
    the caller's config file, not its own. The reply is one line
    ``{"status", "message"}`` with a status of :data:`STATUSES`.
    Raises :class:`Unavailable` when no front end is named or it can't be
    reached, and when it declines: it closes without a reply, or replies with
    anything else (``{"status": "declined"}``, say), so the popup opens instead.
    Raises :class:`NoAnswer` when there is no reply within ``timeout`` seconds,
    which the human had to log in.
    """
    address = os.environ.get(BRIDGE_VARIABLE)
    if not address:
        raise Unavailable
    fields = {"cluster": cluster_name, "session_limit": default_limit, "config": config, "timeout": timeout}
    request = json.dumps(fields).encode("utf-8") + b"\n"
    try:
        reply = _exchange(address, request, timeout)
    except TimeoutError:
        raise NoAnswer from None
    except OSError:
        raise Unavailable from None
    try:
        data = json.loads(reply)
    except ValueError:
        raise Unavailable from None
    if not isinstance(data, dict) or data.get("status") not in STATUSES:
        raise Unavailable
    message = data.get("message")
    return Outcome(data["status"], message if isinstance(message, str) else "")


def _exchange(address: str, request: bytes, timeout: float) -> bytes:
    """Send ``request`` to the front end and return its reply line; ``TimeoutError`` after ``timeout`` seconds."""
    if sys.platform == "win32":
        return _exchange_pipe(address, request, timeout)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(address)
        sock.sendall(request)
        with sock.makefile("rb") as reader:
            return reader.readline()


def _exchange_pipe(address: str, request: bytes, timeout: float) -> bytes:
    """:func:`_exchange` over a Windows named pipe, whose reads take no timeout: a
    daemon thread talks to it, and is left behind if the front end never answers."""
    done: dict = {}

    def talk() -> None:
        try:
            with open(address, "r+b", buffering=0) as pipe:
                pipe.write(request)
                done["reply"] = pipe.readline()
        except OSError as exc:
            done["error"] = exc

    thread = threading.Thread(target=talk, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError
    if "error" in done:
        raise done["error"]
    return done["reply"]
