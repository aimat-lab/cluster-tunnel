# cluster-tunnel (`ctun`) — Technical Design

> Companion to [PLAN.md](./PLAN.md). PLAN states *the problem and the why*; this
> document is *the concrete specification* — command surface, SSH mechanics,
> session model, budget guard, config schema, and the implementation layout.
>
> **Status: design, not yet implemented.** Nothing here is code yet.

---

## 1. Architecture at a glance

```
                          euler (always-on controller, agents run here)
  ┌───────────────────────────────────────────────────────────────────┐
  │                                                                     │
  │   coding agent ──►  ctun  (stateless CLI, invoked per command)      │
  │                      │                                              │
  │      ┌───────────────┼───────────────────────────┐                 │
  │      ▼               ▼                            ▼                 │
  │  config.yaml    session cache              ssh master process      │
  │  (~/.config)    (~/.cache, internal)       (holds the AUTH'd conn) ─┼──► HPC login node
  │                                            via control socket       │       (sshd)
  └───────────────────────────────────────────────────────────────────┘
```

Key property: **`ctun` is stateless and short-lived.** It is launched once per
command and exits. All persistence lives in three places:

| State | Where | Lifetime |
|---|---|---|
| The authenticated connection | a backgrounded `ssh` **master** process + its **control socket** | until the tunnel dies / `logout` / reboot |
| Per-cluster config | `~/.config/cluster-tunnel/config.yaml` | permanent, user-edited |
| Per-cluster **session** state | `~/.cache/cluster-tunnel/sessions/<cluster>.json` | until re-auth overwrites it |

There is **no `ctun` daemon**; we lean entirely on OpenSSH's own master process
for connection persistence.

---

## 2. SSH connection multiplexing (the tunnel)

Built on OpenSSH `ControlMaster` / `ControlPath` / `ControlPersist`. Authentication
is a property of the *connection*, not of each command, so a single OTP login is
reused by every later command as a multiplexed channel — no re-auth.

Paths (defaults, configurable):

- Control socket: `~/.cache/cluster-tunnel/sockets/<cluster>`
- Connection target: a `ssh_alias` from `~/.ssh/config` if given, else `user@host`.

### 2.1 The four ssh invocations `ctun` issues

| `ctun` action | Underlying ssh |
|---|---|
| **open master** (`login`) | `ssh -M -S <sock> -o ControlPersist=<ttl> -o ServerAliveInterval=<n> -o ServerAliveCountMax=3 -fN <target>` |
| **run a command** (`run`) | `ssh -S <sock> -o BatchMode=yes <target> <remote-cmd>` |
| **check liveness** (`status`) | `ssh -O check -S <sock> <target>` |
| **close master** (`logout`) | `ssh -O exit -S <sock> <target>` |

- `-f` backgrounds the master *after* authentication; `-N` runs no remote command —
  the master just holds the connection open, idling at ~0 % CPU.
- `BatchMode=yes` on `run` is the safety latch: if the socket is dead, ssh **fails
  immediately instead of falling back to a password prompt** — this is what makes
  `run` "fail loudly/closed" rather than hang in front of a non-present human.
- `ServerAliveInterval` keepalives fight login-node idle disconnects and keep
  NAT/firewall state warm.

### 2.2 Forwarding the command

`run` captures everything after `--` as a token list and reconstructs the remote
command with `shlex.join`, then hands it to ssh. stdout/stderr are streamed live and
the **remote exit code is propagated** as `ctun`'s exit code. `--tty` (optional) adds
`-tt` for interactive remote programs.

### 2.3 What can end a tunnel (and the response)

| Cause | `ctun` behaviour |
|---|---|
| `ControlPersist` TTL / server idle timeout | next `run` fails closed → agent requests `login --interactive` |
| network drop / euler reboot | same |
| site max-session policy | same — re-auth is unavoidable because OTP needs a human |
| `>MaxSessions` concurrent channels (default 10) | extra concurrent `run`s queue briefly |

---

## 3. Command surface

`-t/--target <cluster>` is a **group-level** option (consumed before the
subcommand), so `ctun -t horeka run -- squeue --me` works and commands that don't
need a cluster (`config`, top-level `status`) ignore it.

| Group | Command | Behaviour | Options |
|---|---|---|---|
| **Tunnel** | `login` | Enter OTP once; open the master; **start a session** (stamp start time + limit into the session cache). | `--interactive`, `--limit N`, `--timeout S` |
| | `status` | No `-t`: table of all clusters (tunnel up?, session usage/limit). With `-t`: detail. | `--json` |
| | `logout` | `ssh -O exit`; clears the session cache for that cluster. | |
| **Execution** | `run` | Run a command through the tunnel, budget-guarded. **`--`-only** syntax. | `--tty`, `--dry-run` |
| **Context** | `info` | Agent briefing: `description` + advisory `restrictions` + budget state. Works with no live tunnel. | `--json` |
| **Config** | `config` | Open `config.yaml` in `$EDITOR` (default action). | `--show`, `--path`, `--validate`, `--init` |
| **Interface** | `webui` | *(future)* launch local Flask + JS app. | `--host`, `--port`, `--no-browser` |

Notes:

- **`run` is `--`-only:** `ctun -t horeka run -- sbatch --time=01:00:00 train.sh`.
  The command sets `context_settings={"ignore_unknown_options": True}` and captures
  `nargs=-1, type=UNPROCESSED`, so remote flags are forwarded verbatim, never parsed
  as `ctun` options. There is no quoted-string form.
- **`status` vs `info` are separate by design.** `status` = live operational state
  (socket up?, channels in use, current usage vs limit). `info` = the agent-facing
  briefing (static description + restrictions + budget), readable before any tunnel
  exists.
- **`run --dry-run`** performs the classify + budget-probe steps and reports the
  verdict without executing the command.
- The budget guard is **non-bypassable** — there is intentionally no `--no-guard`.

### 3.1 `login --interactive` (agent-triggerable human auth)

So an agent can *summon* a human to authenticate without ever seeing the secret:

1. `ctun` pops a small **tkinter dialog** asking the present human for the
   password/OTP and the session limit. The dialog runs as a **subprocess** under a
   Python whose Tk actually renders on this display (candidates probed, system
   Python first) and returns the entered values to `ctun` over a **pipe** — never
   via argv or disk.
2. `ctun` opens the SSH master inside a **pseudo-terminal** (`pty.fork`) and types
   the password in at ssh's prompt. The master uses `-f`, so after authentication
   it backgrounds and persists independently of `ctun` (and of any window).
3. `ctun` blocks, polling `ssh -O check -S <sock> <target>` until the socket is
   live (success) or `--timeout` elapses (failure), then stamps the session with
   the limit from the dialog.

> **Implementation notes.** Routing the master through a terminal emulator was
> fragile — the emulator's systemd scope tears the master down on close (even with
> `nohup`/`setsid`). Driving a detached pty directly avoids that. Tk is also
> finicky: some interpreters' Tk builds abort on this machine's X server (GTK apps
> are unaffected), so the dialog is launched under a probed, known-good Python. If
> no display / working Tk is found, `ctun` falls back to an inline TTY prompt (for a
> human running `login` directly). The password is fed only to ssh's prompt; for
> multi-prompt 2FA only the first prompt is auto-answered (refine per cluster). The
> `defaults.terminal` config key is retained for forward-compat but is unused.

---

## 4. Session model

**A session = the lifetime of one authentication, per cluster.** It begins when OTP
is entered (`login`) and is replaced only when OTP is entered again (re-auth after
the tunnel died or `logout`). There is **no env var, no explicit `session start`, and
no manual reset** — the only way to a fresh budget window is a new OTP login (a human
is always present for that).

On `login`, `ctun` writes:

```
~/.cache/cluster-tunnel/sessions/<cluster>.json
{
  "session_id":  "horeka-1750800000",
  "start_epoch": 1750800000,          // UTC seconds; the budget "since" point
  "limit":       500,                 // from --limit, else config session_limit, else null
  "unit":        "core-hours"         // display only, mirrors config
}
```

- Re-auth **re-stamps `start_epoch`** (usage window zeroes) but reuses the previous
  `limit` unless `--limit` overrides.
- `limit == null` (no `--limit`, no config `session_limit`) ⇒ that cluster is
  **unguarded**: `run` tunnels everything straight through.
- Cache is **internal only** — no `ctun cache` CLI, and budget scripts (which run
  remotely, §5) receive their inputs as arguments, not via the cache.

---

## 5. Budget guard (threshold, session-scoped)

Before executing a command, `ctun`:

1. **Classify.** If the command's first token ∈ `budget.guard_commands`
   (`[sbatch, srun, salloc]`), it is a *submission* and is gated. Otherwise it runs
   immediately, unguarded.
2. **Probe (remote).** Pipe the cluster's bash script through the live tunnel and run
   it *on the login node*, passing the session start as an argument:
   ```
   ssh -S <sock> -o BatchMode=yes <target> \
       "bash -s -- <start_epoch> <cluster> <user>" \
       < ~/.config/cluster-tunnel/budget/<cluster>.sh
   ```
   The script prints **one number** to stdout = compute used since `start_epoch`, in
   the cluster's unit. It runs where `sacct`/`sreport`/site tools live, so it owns all
   querying and parsing.
3. **Compare.** `used ≥ limit` → **hard block**: print
   `BLOCKED: horeka session used 512/500 core-hours — sbatch not submitted`, exit
   non-zero, command never reaches the scheduler. Else forward it.
4. **Fail mode.** Non-zero script exit / unparseable output ⇒ `fail_mode`:
   `closed` (default) blocks; `open` allows. Closed is the principled default for a
   safety limiter.

This is a **threshold** gate, deliberately *not* a forecaster: it blocks *new*
submissions once usage is already at/over the limit; it does not estimate whether a
*specific* job will overshoot. A job submitted at 99 % can still cross the line; the
guard refuses the *next* one.

### 5.1 Budget-script contract

- **Location:** `~/.config/cluster-tunnel/budget/<cluster>.sh` (convention: by cluster
  name; overridable via `budget.script`).
- **Execution:** remote, in the login shell, via `bash -s` over the tunnel.
- **Arguments:** `$1` = session start (UTC epoch seconds), `$2` = cluster name,
  `$3` = remote username.
- **Output:** a single numeric value (int or float) on stdout = used-since-start, in
  the unit declared in config. Exit 0 on success; non-zero ⇒ probe failure.

**Illustrative** template (you must supply/verify the real accounting per cluster):

```bash
#!/usr/bin/env bash
# budget/horeka.sh — CPU core-hours consumed since session start.
# Args: $1=start_epoch  $2=cluster  $3=user
set -euo pipefail
start="$(date -d "@$1" +%Y-%m-%dT%H:%M:%S)"
# core-hours = Σ (AllocCPUS × ElapsedRaw[s]) / 3600 over the user's allocations.
sacct -u "$3" -S "$start" -X -n -P -o AllocCPUS,ElapsedRaw \
  | awk -F'|' '{ s += $1 * $2 } END { printf "%.3f\n", s/3600 }'
```

---

## 6. Configuration schema

Per-user YAML at `~/.config/cluster-tunnel/config.yaml` (override with `$CTUN_CONFIG`
or `--config`). Read with `yaml.safe_load`; if the future `webui` writes it back while
preserving comments, switch the loader to `ruamel.yaml`.

```yaml
defaults:
  control_persist: "12h"        # master lifetime; "yes" = until reboot/logout
  server_alive_interval: 60     # keepalive seconds, fights idle disconnects
  socket_dir: "~/.cache/cluster-tunnel/sockets"
  terminal: auto                # login --interactive popup: auto|gnome-terminal|xfce4-terminal|zenity

agent:
  preamble: |                   # prepended to every cluster's `info` briefing
    You operate on shared academic HPC clusters via `ctun`. Respect the stated
    restrictions and read `ctun -t <cluster> info` before submitting jobs.

clusters:
  horeka:
    host: horeka.scc.kit.edu        # the cluster login URL
    user: ab1234
    ssh_alias: horeka               # optional: reuse a ~/.ssh/config Host block instead
    identity_file: ~/.ssh/horeka    # optional
    control_persist: "24h"          # optional per-cluster override
    description: |
      HoreKa @ KIT (NHR). GPU jobs -> partition 'accelerated' (4xA100/H100 per node).
      Batch via sbatch; large interactive srun discouraged. Node-local scratch: $TMPDIR.
    restrictions:                   # ADVISORY only — surfaced via `info`, never enforced
      max_job_runtime: "48:00:00"
      max_gpus_per_job: 4
      max_concurrent_jobs: 8
      allowed_partitions: [accelerated, cpuonly]
      notes: "Never run compute on the login node."
    budget:                         # omit this block -> cluster runs unguarded
      script: budget/horeka.sh      # remote script (default: budget/<cluster>.sh)
      session_limit: 500            # default if `login --limit` not given; omit -> unguarded
      unit: core-hours              # display only
      guard_commands: [sbatch, srun, salloc]
      fail_mode: closed             # closed = block on probe error (default) | open

  bwuni:
    ssh_alias: bwuni                # already in ~/.ssh/config (uc2.scc.kit.edu, user ab1234)
    description: "bwUniCluster 2.0 @ KIT. GPU partitions gpu_h100 / gpu_a100."
    # no budget block -> no core-hour budget; tunnel+run unguarded

webui:                              # future `ctun webui`
  host: 127.0.0.1
  port: 8765
  open_browser: true
```

Resolution rules:

- `ssh_alias` wins if present (ssh resolves host/user/identity from `~/.ssh/config`);
  otherwise `host` + `user` + `identity_file`.
- Per-cluster keys override `defaults`.
- Single-quoted YAML scalars keep backslashes literal — relevant if a script path or
  value ever needs them.

---

## 7. On-disk layout

```
~/.config/cluster-tunnel/
├── config.yaml
└── budget/
    ├── horeka.sh
    ├── juwels.sh
    └── …                      # one remote probe script per guarded cluster

~/.cache/cluster-tunnel/
├── sockets/
│   └── <cluster>             # ssh ControlPath sockets
└── sessions/
    └── <cluster>.json        # internal session state (start_epoch, limit, unit)
```

---

## 8. Implementation layout (rich-click mixin pattern)

```
cluster-tunnel/
├── pyproject.toml                    # [project.scripts] ctun = "cluster_tunnel.cli:cli"
├── PLAN.md   DESIGN.md   README.md
└── src/cluster_tunnel/
    ├── cli/
    │   ├── __init__.py               # CLI class (mixin composition), create_cli(), `cli`
    │   ├── base.py                   # BaseCLI(click.RichGroup) — grouped help panels
    │   ├── display.py                # RichLogo, RichHelp
    │   ├── constants.py              # default paths, guard defaults, terminal order
    │   ├── tunnel.py                 # TunnelCommandsMixin   -> login, status, logout
    │   ├── execution.py              # ExecutionCommandsMixin-> run
    │   ├── context.py                # ContextCommandsMixin  -> info
    │   ├── configcmd.py              # ConfigCommandsMixin   -> config
    │   └── webui.py                  # WebuiCommandsMixin (placeholder)
    ├── config.py                     # YAML load + validate; Config/Cluster dataclasses
    ├── ssh.py                        # ControlMaster wrapper: open_master/run/check/exit
    ├── session.py                    # session-cache read/write
    ├── budget.py                     # remote script runner + threshold decision
    └── popup.py                      # terminal/zenity window spawning for --interactive
```

- `-t/--target` is parsed by the group callback and stashed (e.g.
  `ctx.meta["target"]`); command mixins resolve the `Cluster` from `Config`.
- Heavy/optional imports (YAML, the webui's Flask) are late-imported inside command
  bodies to keep `--help` fast.
- `COMMAND_GROUPS` drives the grouped help panels:
  `Tunnel [login, status, logout]`, `Execution [run]`, `Context [info]`,
  `Config [config]`, `Interface [webui]`.

Dependencies: `rich-click`, `rich`, `PyYAML`. (`python-dotenv` optional.)

---

## 9. Decisions recap

| Topic | Decision |
|---|---|
| Form factor | CLI prefix tool `ctun`, not an MCP server |
| Persistence | OpenSSH ControlMaster/ControlPersist; no custom daemon |
| Cluster selector | group option `-t/--target` |
| `run` syntax | `--`-only passthrough, exit code propagated |
| Auth | `login` (human) + `login --interactive` (agent → blocking popup) |
| Dead tunnel | `run` fails closed (BatchMode) |
| Session | = one authentication's lifetime, per cluster; resets only on re-auth |
| Budget metric | per-cluster **remote bash script**, prints used-since-session-start |
| Budget enforcement | threshold hard block when `used ≥ limit`; fail closed on probe error |
| Limit source | `login --limit`, else config `session_limit`, else unguarded |
| Restrictions | advisory only (surfaced via `info`), never enforced |
| Config | per-user YAML `~/.config/cluster-tunnel/config.yaml` |
| Cache | internal-only session state; no general KV store |
| Stack | Python + rich-click (mixin layout), uv |

---

## 10. Deferred / open

- **Briefing delivery** — `info` emits the briefing; *how* it reaches an agent on
  startup (CLAUDE.md, a generated `AGENTS.md`, a hook) is decided later.
- **Budget scripts** — the real per-cluster accounting commands + sample outputs are
  still to be provided; the §5.1 template is illustrative only.
- **`webui`** — local Flask + JS interface; placeholder mixin now.
- **Probe caching** — optional few-second cache if an agent loops submissions rapidly.
- **`login --interactive` polish** — multi-prompt OTP UX, timeout UX, failure
  surfacing; validate against a real OTP cluster (only key-auth `localhost` has been
  exercised so far).
```
