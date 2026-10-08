"""Interactive (OTP) login via a self-contained tkinter dialog + a pty-driven master.

When an agent runs ``ctun ... login --interactive`` there is no human at ctun's
terminal. We pop a small **tkinter** window asking the present human for the
password/OTP and the session limit, then drive the SSH master inside a
**pseudo-terminal**, typing the password into it at the prompt
(:mod:`cluster_tunnel.pty_login`). The master is started with ``-f`` so it
backgrounds after authentication and persists independently of ctun. On Windows
that pty driver runs inside WSL, next to ssh.

The dialog is launched as a subprocess under a Python whose Tk actually renders on
this display: some interpreters' Tk builds abort on certain X servers, so we probe
candidates (system python first) and use the first that renders. The password is
returned to ctun over a pipe (never via argv or disk).
"""

from __future__ import annotations

import base64
import functools
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from typing import Optional

from cluster_tunnel import ssh, wsl
from cluster_tunnel.ssh import ConnSpec

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
requires_otp = (sys.argv[5] != "0") if len(sys.argv) > 5 else True
requires_password = (sys.argv[6] != "0") if len(sys.argv) > 6 else True

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

row = 2
first_entry = None
# The password field is shown only when the cluster uses a service password;
# otherwise `pw` stays empty and no password prompt is answered during login.
pw = tk.StringVar()
if requires_password:
    tk.Label(frm, text="Password:").grid(row=row, column=0, sticky="e", padx=(0, 8), pady=4)
    e1 = tk.Entry(frm, show="*", textvariable=pw, width=30)
    e1.grid(row=row, column=1, sticky="we", pady=4)
    first_entry = e1
    row += 1

# The OTP field is shown only when the cluster requires a one-time passcode;
# otherwise `otp` stays empty and no OTP prompt is answered during login.
otp = tk.StringVar()
if requires_otp:
    tk.Label(frm, text="OTP / passcode:").grid(row=row, column=0, sticky="e", padx=(0, 8), pady=4)
    e2 = tk.Entry(frm, show="*", textvariable=otp, width=30)
    e2.grid(row=row, column=1, sticky="we", pady=4)
    if first_entry is None:
        first_entry = e2
    row += 1

limit_label = "Session limit" + (" (" + unit + ")" if unit else "") + ":"
tk.Label(frm, text=limit_label).grid(row=row, column=0, sticky="e", padx=(0, 8), pady=4)
lim = tk.StringVar(value=default_limit)
lim_entry = tk.Entry(frm, textvariable=lim, width=30)
lim_entry.grid(row=row, column=1, sticky="we", pady=4)
if first_entry is None:
    first_entry = lim_entry
row += 1
err = tk.StringVar()
tk.Label(frm, textvariable=err, fg="red").grid(row=row, column=0, columnspan=2, sticky="w")
row += 1

def submit(event=None):
    # A cluster that uses a password requires a non-empty one; OTP and the
    # session limit may still be left blank.
    if requires_password and not pw.get():
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
btns.grid(row=row, column=0, columnspan=2, pady=(10, 0), sticky="e")
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
if first_entry is not None:
    first_entry.focus_set()

if os.environ.get("CTUN_DIALOG_AUTOTEST"):  # test hook: auto-fill + submit
    if requires_password:
        pw.set(os.environ["CTUN_DIALOG_AUTOTEST"])
    if requires_otp:
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
    # Windows has no system python3, and a bare `python3` on PATH may be the
    # Microsoft Store stub; the running interpreter ships a working Tk there.
    if sys.platform == "win32":
        cands: tuple[Optional[str], ...] = (sys.executable,)
    else:
        cands = ("/usr/bin/python3", shutil.which("python3"), sys.executable)
    out: list[str] = []
    for cand in cands:
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
    """True if a tkinter dialog can be shown (a display and a working Tk exist).

    X11/Wayland sessions advertise their display in the environment; Windows has
    no such variable, as Tk draws on the desktop directly.
    """
    if sys.platform != "win32" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False
    return _dialog_python() is not None


def prompt_credentials(
    cluster_name: str,
    target: str,
    default_limit: Optional[float],
    unit: str = "units",
    requires_otp: bool = True,
    requires_password: bool = True,
) -> Optional[Credentials]:
    """Show the blocking tkinter dialog; return Credentials, or None if cancelled.

    ``unit`` is shown in brackets after the session-limit label (e.g. "Session
    limit (jobh):"). When ``requires_otp`` is false the dialog omits the OTP
    field entirely. When ``requires_password`` is false the dialog omits the
    password field entirely (OTP-only or key-based auth); when true, a non-empty
    password must be entered before the dialog submits.
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
        "1" if requires_otp else "0",
        "1" if requires_password else "0",
    ]
    res = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
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


def login_with_password(
    spec: ConnSpec,
    password: str,
    otp: Optional[str],
    timeout: int,
    verbose: int = 0,
) -> bool:
    """Open the SSH master in a pty, answering the password and OTP prompts.

    The prompt-answering loop lives in :mod:`cluster_tunnel.pty_login`. It runs
    in-process here, or inside WSL on Windows, next to the ssh it drives. With
    ``verbose`` > 0, ssh's own diagnostics are printed to stderr if the login
    fails.
    """
    from cluster_tunnel import pty_login

    ssh.prepare_socket(spec)
    master = ssh.open_master_argv(spec, verbose)
    check = ssh.check_argv(spec)
    if wsl.enabled():
        return _login_in_wsl(spec, master, check, password, otp, timeout, verbose)

    live, transcript = pty_login.login(master, check, password, otp, timeout)
    if not live and verbose and transcript:
        sys.stderr.write(transcript.decode("utf-8", "replace"))
    return live


def _login_in_wsl(
    spec: ConnSpec,
    master: list[str],
    check: list[str],
    password: str,
    otp: Optional[str],
    timeout: int,
    verbose: int,
) -> bool:
    """Run :mod:`cluster_tunnel.pty_login` inside WSL via ``python3 -I -``.

    cluster_tunnel isn't installed in WSL, so the module's source is streamed on
    stdin with a call to its ``main()`` appended. The secrets ride base64-encoded
    inside that program text, never in argv or the environment: the backgrounded
    master would keep its environment for its whole lifetime. ``-I`` keeps the
    working directory (the user's project, translated) off ``sys.path``, so a
    module planted there can't run before the secrets are decoded.
    """
    payload = json.dumps(
        {
            "master_argv": master,
            "check_argv": check,
            "password": password,
            "otp": otp,
            "timeout": timeout,
            "verbose": verbose,
        }
    )
    encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    source = resources.files("cluster_tunnel").joinpath("pty_login.py").read_text(encoding="utf-8")
    program = f"{source}\nraise SystemExit(main({encoded!r}))\n"
    try:
        res = wsl.run(
            ["python3", "-I", "-"], spec.wsl_distro, stdin=program.encode("utf-8"),
            timeout=timeout + 60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if res.returncode != 0 and verbose and res.stderr:
        sys.stderr.write(res.stderr)
    return res.returncode == 0
