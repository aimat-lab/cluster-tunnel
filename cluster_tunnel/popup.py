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

_PROMPT_KEYS = (
    b"password",
    b"passcode",
    b"passphrase",
    b"verification",
    b"one-time",
    b"otp",
    b"token",
)

# Renders a throwaway window; exits 0 only if this interpreter's Tk works here.
_RENDER_PROBE = "import tkinter as tk; r=tk.Tk(); tk.Label(r,text='x').pack(); r.update(); r.destroy()"

# Standalone tkinter dialog, run as a subprocess. Prints {"password","limit"} JSON
# to stdout on submit, exits non-zero on cancel.
_DIALOG_SCRIPT = r"""
import os, sys, json
import tkinter as tk

cluster = sys.argv[1] if len(sys.argv) > 1 else "cluster"
target = sys.argv[2] if len(sys.argv) > 2 else ""
default_limit = sys.argv[3] if len(sys.argv) > 3 else ""

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
tk.Label(frm, text="Password / OTP:").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=4)
pw = tk.StringVar()
e1 = tk.Entry(frm, show="*", textvariable=pw, width=30)
e1.grid(row=2, column=1, sticky="we", pady=4)
tk.Label(frm, text="Session limit:").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=4)
lim = tk.StringVar(value=default_limit)
tk.Entry(frm, textvariable=lim, width=30).grid(row=3, column=1, sticky="we", pady=4)
err = tk.StringVar()
tk.Label(frm, textvariable=err, fg="red").grid(row=4, column=0, columnspan=2, sticky="w")

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
    state["creds"] = {"password": pw.get(), "limit": limit}
    root.quit()

def cancel(event=None):
    state["creds"] = None
    root.quit()

btns = tk.Frame(frm)
btns.grid(row=5, column=0, columnspan=2, pady=(10, 0), sticky="e")
tk.Button(btns, text="Cancel", command=cancel).pack(side="right", padx=(6, 0))
tk.Button(btns, text="Login", command=submit, default="active").pack(side="right")
root.bind("<Return>", submit)
root.bind("<Escape>", cancel)
root.protocol("WM_DELETE_WINDOW", cancel)
e1.focus_set()

if os.environ.get("CTUN_DIALOG_AUTOTEST"):  # test hook: auto-fill + submit
    pw.set(os.environ["CTUN_DIALOG_AUTOTEST"])
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
    cluster_name: str, target: str, default_limit: Optional[float]
) -> Optional[Credentials]:
    """Show the blocking tkinter dialog; return Credentials, or None if cancelled."""
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
    ]
    res = subprocess.run(args, capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        data = json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return Credentials(password=data["password"], limit=data.get("limit"))


def _looks_like_prompt(buf: bytes) -> bool:
    low = buf.lower()
    return any(key in low for key in _PROMPT_KEYS)


def login_with_password(spec: ConnSpec, password: str, timeout: int) -> bool:
    """Open the SSH master in a pty, typing `password` at the prompt; wait for live."""
    spec.socket.parent.mkdir(parents=True, exist_ok=True)
    ssh.ensure_clean_socket(spec)
    argv = ssh.open_master_argv(spec)

    pid, fd = pty.fork()
    if pid == 0:  # child: become the ssh master, attached to the pty
        try:
            os.execvp(argv[0], argv)
        except Exception:
            os._exit(127)

    deadline = time.time() + timeout
    sent = False
    buf = b""
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
                if not sent and _looks_like_prompt(buf):
                    try:
                        os.write(fd, password.encode() + b"\n")
                    except OSError:
                        break
                    sent = True
                    buf = b""
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
    for _ in range(6):
        if ssh.is_live(spec):
            return True
        time.sleep(0.3)
    return ssh.is_live(spec)
