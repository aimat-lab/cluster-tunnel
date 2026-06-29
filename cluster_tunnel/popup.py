"""Interactive (OTP) login via a self-contained tkinter dialog + a pty-driven master.

When an agent runs ``ctun ... login --interactive`` there is no human at ctun's
terminal. We pop a small **tkinter** window asking the present human for the
password/OTP and the session limit, then drive the SSH master inside a
**pseudo-terminal**, typing the password into it at the prompt. The master is
started with ``-f`` so it backgrounds after authentication and persists
independently of ctun.

The dialog is launched as a subprocess under a Python whose Tk actually renders on
this display: some interpreters' Tk builds abort on certain X servers, so we probe
candidates (system python first) and use the first that renders. The password is
returned to ctun over a pipe (never via argv or disk).
"""

from __future__ import annotations

import functools
import json
import os
import pty
import select
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

from cluster_tunnel import ssh
from cluster_tunnel.ssh import ConnSpec

# Cluster login asks for two distinct secrets — the service password and the
# one-time passcode (OTP) — in a sequence whose order varies by site. We classify
# each prompt by its wording and answer it with the matching secret. OTP keys are
# checked first because they are the more specific signal.
_OTP_KEYS = (
    b"passcode",
    b"verification",
    b"one-time",
    b"otp",
    b"token",
    b"second factor",
    b"2fa",
)
_PASSWORD_KEYS = (
    b"password",
    b"passphrase",
)
_PROMPT_KEYS = _OTP_KEYS + _PASSWORD_KEYS

# Renders a throwaway window; exits 0 only if this interpreter's Tk works here.
_RENDER_PROBE = "import tkinter as tk; r=tk.Tk(); tk.Label(r,text='x').pack(); r.update(); r.destroy()"

# Standalone tkinter dialog, run as a subprocess. Prints
# {"password","otp","limit"} JSON to stdout on submit, exits non-zero on cancel.
_DIALOG_SCRIPT = r"""
import os, sys, json
import tkinter as tk

cluster = sys.argv[1] if len(sys.argv) > 1 else "cluster"
target = sys.argv[2] if len(sys.argv) > 2 else ""
default_limit = sys.argv[3] if len(sys.argv) > 3 else ""
unit = sys.argv[4] if len(sys.argv) > 4 else ""

state = {"creds": None}
root = tk.Tk()
root.title("ctun login - " + cluster)
try:
    root.attributes("-topmost", True)
except tk.TclError:
    pass

frm = tk.Frame(root, padx=16, pady=12)
frm.pack(fill="both", expand=True)
tk.Label(frm, text="Authenticate to " + cluster, font=("", 11, "bold")).grid(
    row=0, column=0, columnspan=2, sticky="w")
tk.Label(frm, text=target, fg="#666").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

tk.Label(frm, text="Password:").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=4)
pw = tk.StringVar()
e1 = tk.Entry(frm, show="*", textvariable=pw, width=30)
e1.grid(row=2, column=1, sticky="we", pady=4)

tk.Label(frm, text="OTP / passcode:").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=4)
otp = tk.StringVar()
e2 = tk.Entry(frm, show="*", textvariable=otp, width=30)
e2.grid(row=3, column=1, sticky="we", pady=4)

limit_label = "Session limit" + (" (" + unit + ")" if unit else "") + ":"
tk.Label(frm, text=limit_label).grid(row=4, column=0, sticky="e", padx=(0, 8), pady=4)
lim = tk.StringVar(value=default_limit)
tk.Entry(frm, textvariable=lim, width=30).grid(row=4, column=1, sticky="we", pady=4)
err = tk.StringVar()
tk.Label(frm, textvariable=err, fg="red").grid(row=5, column=0, columnspan=2, sticky="w")

def submit(event=None):
    if not pw.get():
        err.set("Password required.")
        return
    text = lim.get().strip()
    limit = None
    if text:
        try:
            limit = float(text)
        except ValueError:
            err.set("Limit must be a number (or blank).")
            return
    state["creds"] = {"password": pw.get(), "otp": otp.get(), "limit": limit}
    root.quit()

def cancel(event=None):
    state["creds"] = None
    root.quit()

btns = tk.Frame(frm)
btns.grid(row=6, column=0, columnspan=2, pady=(10, 0), sticky="e")
tk.Button(btns, text="Cancel", command=cancel).pack(side="right", padx=(6, 0))
login_btn = tk.Button(
    btns, text="Login", command=submit, default="active",
    bg="#a6e3a1", activebackground="#94d68f", fg="#14341a",
    activeforeground="#14341a",
)
login_btn.pack(side="right")
# Enter submits from anywhere in the dialog; Escape / window-close cancels.
root.bind("<Return>", submit)
root.bind("<KP_Enter>", submit)
root.bind("<Escape>", cancel)
root.protocol("WM_DELETE_WINDOW", cancel)
e1.focus_set()

if os.environ.get("CTUN_DIALOG_AUTOTEST"):  # test hook: auto-fill + submit
    pw.set(os.environ["CTUN_DIALOG_AUTOTEST"])
    otp.set(os.environ.get("CTUN_DIALOG_AUTOTEST_OTP", ""))
    root.after(400, submit)

root.mainloop()
try:
    root.destroy()
except tk.TclError:
    pass
if state["creds"] is None:
    sys.exit(1)
sys.stdout.write(json.dumps(state["creds"]))
"""


@dataclass
class Credentials:
    password: str
    otp: Optional[str]
    limit: Optional[float]


def _candidate_pythons() -> list[str]:
    out: list[str] = []
    for cand in ("/usr/bin/python3", shutil.which("python3"), sys.executable):
        if cand and cand not in out and os.path.exists(cand):
            out.append(cand)
    return out


@functools.lru_cache(maxsize=1)
def _dialog_python() -> Optional[str]:
    """First interpreter whose Tk actually renders on this display."""
    for py in _candidate_pythons():
        try:
            res = subprocess.run([py, "-c", _RENDER_PROBE], capture_output=True, timeout=10)
            if res.returncode == 0:
                return py
        except Exception:
            continue
    return None


def gui_available() -> bool:
    """True if a tkinter dialog can be shown (a display and a working Tk exist)."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return _dialog_python() is not None


def prompt_credentials(
    cluster_name: str, target: str, default_limit: Optional[float], unit: str = "units"
) -> Optional[Credentials]:
    """Show the blocking tkinter dialog; return Credentials, or None if cancelled.

    ``unit`` is shown in brackets after the session-limit label (e.g. "Session
    limit (gpuh):").
    """
    py = _dialog_python()
    if py is None:
        return None
    args = [
        py,
        "-c",
        _DIALOG_SCRIPT,
        cluster_name,
        target,
        "" if default_limit is None else str(default_limit),
        unit or "",
    ]
    res = subprocess.run(args, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        data = json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return Credentials(
        password=data["password"],
        otp=(data.get("otp") or None),
        limit=data.get("limit"),
    )


def _looks_like_prompt(buf: bytes) -> bool:
    low = buf.lower()
    return any(key in low for key in _PROMPT_KEYS)


def _classify_prompt(buf: bytes) -> Optional[str]:
    """Classify the most recent prompt as ``"otp"``, ``"password"``, or ``None``.

    OTP keywords are tested first: an OTP prompt rarely contains "password", but a
    banner or password prompt could mention a token, so the more specific signal
    wins.
    """
    low = buf.lower()
    if any(key in low for key in _OTP_KEYS):
        return "otp"
    if any(key in low for key in _PASSWORD_KEYS):
        return "password"
    return None


def login_with_password(
    spec: ConnSpec,
    password: str,
    otp: Optional[str],
    timeout: int,
    verbose: int = 0,
) -> bool:
    """Open the SSH master in a pty, answering the password and OTP prompts.

    The cluster asks for the service password and the one-time passcode in a
    sequence whose order varies by site; each prompt is classified by its wording
    (:func:`_classify_prompt`) and answered with the matching secret. Each secret
    is sent at most once. A missing/blank ``otp`` simply means OTP prompts go
    unanswered (for clusters that do not use one). With ``verbose`` > 0, ssh's own
    diagnostics are captured and printed to stderr if the login fails.
    """
    spec.socket.parent.mkdir(parents=True, exist_ok=True)
    ssh.ensure_clean_socket(spec)
    argv = ssh.open_master_argv(spec, verbose)

    secrets = {"password": password, "otp": otp}
    sent = {"password": False, "otp": False}

    pid, fd = pty.fork()
    if pid == 0:  # child: become the ssh master, attached to the pty
        try:
            os.execvp(argv[0], argv)
        except Exception:
            os._exit(127)

    deadline = time.time() + timeout
    buf = b""
    transcript = b""
    try:
        while time.time() < deadline:
            if ssh.is_live(spec):
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
                kind = _classify_prompt(buf)
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
    live = ssh.is_live(spec)
    for _ in range(6):
        if live:
            break
        time.sleep(0.3)
        live = ssh.is_live(spec)

    if not live and verbose and transcript:
        sys.stderr.write(transcript.decode("utf-8", "replace"))
    return live
