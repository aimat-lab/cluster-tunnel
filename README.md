# cluster-tunnel (`ctun`)

`ctun` keeps an authenticated SSH connection to an OTP-protected HPC cluster **alive
in the background**, so you (and your coding agents) can run commands on the cluster
**without re-entering a one-time password every time** — and it enforces a
**per-session compute budget** so automated job submission can't overspend.

You authenticate once. After that, every `ctun -t <cluster> run -- <command>` reuses
the live connection with zero re-authentication, and any job submission is checked
against a budget before it reaches the scheduler.

```console
$ ctun -t horeka login                 # enter password + OTP once
$ ctun -t horeka run -- squeue --me     # reuse the live tunnel, no re-auth
$ ctun -t horeka run -- sbatch train.sh # submitted only if within budget
$ ctun -t horeka status                 # tunnel state + budget used
$ ctun -t horeka logout                 # close the tunnel
```

---

## Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Install](#install)
- [Quickstart](#quickstart)
- [Commands](#commands)
- [Configuration](#configuration)
- [The compute-budget guard](#the-compute-budget-guard)
- [Using ctun with coding agents](#using-ctun-with-coding-agents)
- [Files and locations](#files-and-locations)

---

## How it works

`ctun` is built on OpenSSH **connection multiplexing** (`ControlMaster` /
`ControlPersist`). Authentication is a property of the *connection*, not of each
command:

- **`login`** opens a single **master connection** and leaves a control socket alive
  in the background. This is the only step that needs your password + OTP.
- Every later **`run`** opens a lightweight channel *inside* that already-authenticated
  connection — no password, no OTP, near-instant.

```
   your machine                                  cluster login node
  ┌───────────────────────────────┐
  │  ssh master ───── authenticated connection ─────────►  sshd
  │     ▲  (idle, persistent)      │   (channels multiplexed over it)
  │     │ control socket           │
  │  ctun run ── new channel ──────┼──►  squeue / sbatch / …  (no re-auth)
  └───────────────────────────────┘
```

`ctun` itself is **stateless** — it runs once per command and exits. The persistent
state lives in the background ssh master, a small config file, and a per-cluster
session record. The connection lives as long as the network and the cluster's session
policy allow; when it eventually drops, `run` fails with a clear message and you simply
`login` again.

---

## Requirements

- **Linux or macOS** with an **OpenSSH client** (`ssh`) — version 7.x+.
- **Python ≥ 3.10**.
- For the optional pop-up login dialog (`login --interactive`): a graphical display and
  a working **Tk** (provided by your system Python's `tkinter`).

---

## Install

Using [uv](https://docs.astral.sh/uv/) (recommended — installs `ctun` onto your PATH):

```console
$ git clone <repo-url> cluster-tunnel
$ cd cluster-tunnel
$ uv tool install .
$ ctun --help
```

Or with pip:

```console
$ pip install .         # into the current environment
```

For local development:

```console
$ uv sync               # create .venv and install
$ uv run ctun --help
$ uv run pytest         # run the test suite
```

---

## Quickstart

**1. Create your config:**

```console
$ ctun config --init
Config ready at /home/you/.config/cluster-tunnel/config.yaml
```

**2. Add your cluster** (`ctun config` opens it in `$EDITOR`):

```yaml
clusters:
  horeka:
    host: horeka.scc.kit.edu
    user: ab1234
    description: |
      HoreKa @ KIT. GPU jobs go to partition 'accelerated'.
```

**3. Log in once** (enter your password + OTP at the prompt):

```console
$ ctun -t horeka login
Tunnel to 'horeka' established.
Session budget: unguarded (no limit set).
```

**4. Run commands through the tunnel** — note the `--`, after which everything is sent
verbatim to the cluster:

```console
$ ctun -t horeka run -- squeue --me
$ ctun -t horeka run -- sbatch train.sh
```

**5. Check status, then log out when done:**

```console
$ ctun -t horeka status
$ ctun -t horeka logout
```

---

## Commands

The cluster is selected with the global **`-t` / `--target`** option, placed **before**
the subcommand: `ctun -t <cluster> <command> …`.

Global options:

| Option | Meaning |
|---|---|
| `-t, --target <name>` | Cluster to act on (a key under `clusters:` in the config). |
| `-c, --config <path>` | Use an alternate config file (overrides `$CTUN_CONFIG`). |
| `-V, --verbose` | Enable debug logging. |
| `--version` | Print the version. |

### `login` — authenticate and open the tunnel

```console
$ ctun -t horeka login [-i|--interactive] [-l|--limit N] [--timeout S]
```

- Opens the persistent master connection; prompts for your password + OTP.
- `-l, --limit N` sets this session's compute budget (overrides the cluster's configured
  `session_limit`).
- `-i, --interactive` shows a small pop-up window (password + limit) instead of prompting
  in the terminal, then feeds the password to ssh for you. Useful when `ctun` is invoked by
  a tool that has no terminal of its own (e.g. a coding agent) — a person at the machine
  fills in the window. `--timeout S` bounds how long it waits (default 120s).

A **session** lasts for the lifetime of one authentication. Logging in again (after the
tunnel drops) starts a fresh session and resets the budget window.

### `run` — run a command through the tunnel

```console
$ ctun -t horeka run [--tty] [-n|--dry-run] -- <command> [args…]
```

- Everything after `--` is forwarded to the cluster exactly as written, so flags like
  `--me` or `--time=01:00:00` pass straight through.
- The command's **exit code is propagated** as `ctun`'s exit code, and output is streamed
  live.
- `--tty` allocates a pseudo-terminal for interactive remote programs.
- `-n, --dry-run` reports the budget decision without executing; it exits with the same code
  it *would* have used (see below), so it doubles as a pre-flight check.
- If there is no live tunnel, `run` **fails immediately** with a message telling you to
  `login` — it never silently prompts for a password.

```console
$ ctun -t horeka run -- squeue --me
$ ctun -t horeka run -- bash -c 'cd $WORK && sbatch job.sh'
```

#### Exit codes

So a caller (especially a coding agent) can tell *why* `run` stopped, ctun's own
failures use distinct exit codes and also print a stable `ctun-error: <marker>` line to
stderr. A successful command's own exit code passes straight through, so these codes are
reserved for ctun's pre-flight failures:

| Exit code | `ctun-error:` marker | Meaning |
|---|---|---|
| `10` | `login_required` | No live tunnel — run `login` again. |
| `11` | `budget_exhausted` | A guarded command was blocked: session usage is at/over the limit. |
| `12` | `budget_guard_error` | A guarded command was blocked because the budget could not be verified (fail-closed). |
| *N* | — | The remote command's **own** exit code (0–255), propagated unchanged. |
| `2` | — | Usage error (e.g. no command after `--`). |

```console
$ ctun -t horeka run -- sbatch big.sh ; echo "exit=$?"
ctun-error: budget_exhausted
Error: Submission blocked on 'horeka': compute budget exhausted.
  ...
exit=11
```

### `status` — tunnel and session state

```console
$ ctun -t horeka status              # one cluster (detailed)
$ ctun status                        # all configured clusters
$ ctun -t horeka status -j|--json    # machine-readable
```

Shows whether the tunnel is live, when the session started, and the budget limit.

### `info` — briefing for a cluster

```console
$ ctun -t horeka info [-j|--json]
```

Prints the cluster's description, its (advisory) restrictions, and the current budget
usage vs. limit. Works even without a live tunnel.

### `logout` — close the tunnel

```console
$ ctun -t horeka logout              # one cluster
$ ctun logout                        # all configured clusters
```

Tears down the master connection and clears the session record. Without a
target, it logs out every configured cluster.

To run `ctun logout` **automatically on every logout, reboot, or shutdown**,
install the systemd user hook in [`systemd/`](./systemd/) (`cd systemd &&
./install.sh`). See [systemd/README.md](./systemd/README.md) for details.

### `config` — manage the config file

```console
$ ctun config                  # open in $EDITOR
$ ctun config -i|--init        # create a starter config if none exists
$ ctun config -p|--path        # print the config file path
$ ctun config -s|--show        # print the resolved config
$ ctun config --validate       # check for errors + warn on missing budget
                               # scripts / invalid guard_commands regexes
```

### Shell completion

`ctun` can complete subcommands, flags, and — dynamically from your config — **cluster
names after `-t/--target`**. Enable it once per shell with the top-level
`--init-completion` option (it prints the activation script and exits):

```console
$ ctun --init-completion bash >> ~/.bashrc          # bash
$ ctun --init-completion zsh  >> ~/.zshrc           # zsh
$ ctun --init-completion fish >  ~/.config/fish/completions/ctun.fish   # fish
```

Restart the shell, then `ctun -t <TAB>` offers the clusters from your `config.yaml`
(add a cluster and it completes immediately — the candidate list is read live).

### `webui`

```console
$ ctun webui
```

Placeholder for a planned local web interface (not yet implemented).

---

## Configuration

Config lives at `~/.config/cluster-tunnel/config.yaml` (override with `$CTUN_CONFIG` or
`-c/--config`). A fully annotated example:

```yaml
# Defaults inherited by every cluster (each is overridable per cluster).
defaults:
  control_persist: "12h"          # how long the tunnel stays alive; "yes" = until reboot/logout
  server_alive_interval: 60       # keepalive seconds; fights idle disconnects
  terminal: auto                  # reserved for future use

# Optional preamble surfaced in `ctun info` (e.g. guidance for an agent).
agent:
  preamble: |
    Respect the stated budget and restrictions before submitting jobs.

# One entry per cluster; the key (e.g. "horeka") is the name you pass to -t.
clusters:
  horeka:
    host: horeka.scc.kit.edu      # cluster login host
    user: ab1234                  # your username on the cluster
    ssh_alias: horeka             # optional: reuse a Host block from ~/.ssh/config instead of host/user
    identity_file: ~/.ssh/horeka  # optional: SSH key to use
    control_persist: "24h"        # optional per-cluster override

    description: |
      HoreKa @ KIT. GPU jobs go to partition 'accelerated'.

    restrictions:                 # advisory only — shown by `ctun info`, never enforced
      max_job_runtime: "48:00:00"
      max_gpus_per_job: 4
      allowed_partitions: [accelerated, cpuonly]

    budget:                       # omit this whole block to run the cluster unguarded
      script: budget/horeka.sh    # script that reports usage since the session started
      session_limit: 500          # default budget; overridden by `login --limit`
      unit: jobh                  # label for display (bundled scripts report job-hours)
      guard_commands: [sbatch, srun, salloc]   # commands subject to the budget check
      fail_mode: closed           # if the budget script errors: "closed" blocks, "open" allows

# Planned web interface settings (currently unused).
webui:
  host: 127.0.0.1
  port: 8765
  open_browser: true
```

**Connecting:** if `ssh_alias` is set, `ctun` uses that `~/.ssh/config` Host entry for the
hostname, user, and key; otherwise it uses `host` + `user` (+ optional `identity_file`).

**Restrictions** are informational. They are shown by `ctun info` as guidance but are
never enforced by `ctun` — the only hard enforcement is the budget guard below.

---

## The compute-budget guard

The guard limits how much compute a **session** consumes — where a session is the period
since you last logged in. It is a **threshold** check: once usage reaches the limit, new
job submissions are blocked. (It does not try to predict whether a particular job will
overshoot.)

Before running a command whose first word is in `guard_commands` (`sbatch`, `srun`,
`salloc`), `ctun`:

1. Runs the cluster's **budget script** on the login node (over the live tunnel).
2. Reads the single number it prints — the usage since the session started.
3. Blocks the command if `used ≥ limit`; otherwise lets it run.

Commands that aren't job submissions (`ls`, `squeue`, `sacct`, …) always pass through. If
no limit is set for the cluster, it runs unguarded.

The default metric is **job-hours**: the Slurm job *wall-clock* time you have run since the
session started, counting only the portion of each job inside the session window (concurrent
jobs add up). The bundled budget scripts (`ctun config --init` copies them next to your
config) compute this from `sacct` and work on any cluster with Slurm accounting enabled.

### Writing a budget script

A budget script lives next to your config (e.g.
`~/.config/cluster-tunnel/budget/horeka.sh`) and runs **on the cluster login node**. It
receives three arguments and must print **one number** (the usage since the session start)
to standard output:

| Argument | Value |
|---|---|
| `$1` | session start time (UTC epoch seconds) |
| `$2` | cluster name |
| `$3` | your username on the cluster |

Example — job wall-clock hours since the session started, via Slurm accounting (this is the
bundled default; concurrent jobs add up, each clamped to the session window):

Epochs are computed with `date` (GNU coreutils), not awk's gawk-only `mktime()`, so this
runs under any awk (including mawk, the default on Debian/Ubuntu):

```bash
#!/usr/bin/env bash
set -u
export LC_ALL=C   # locale-independent number formatting (avoid e.g. "2,000")
start_iso="$(date -d "@$1" +%Y-%m-%dT%H:%M:%S)"
now="$(date +%s)"; sum=0
# job-hours = Σ over the user's jobs of time-within-[start, now], from sacct Start/End.
while IFS='|' read -r s e _; do
    [ -n "$s" ] || continue
    st="$(date -d "$s" +%s 2>/dev/null)" || continue
    case "$e" in ''|Unknown|None) en="$now" ;; *) en="$(date -d "$e" +%s 2>/dev/null)"; [ -n "$en" ] || en="$now" ;; esac
    lo="$1"; [ "$st" -gt "$lo" ] && lo="$st"        # clamp to the session window
    hi="$now"; [ "$en" -lt "$hi" ] && hi="$en"
    [ "$hi" -gt "$lo" ] && sum=$(( sum + hi - lo ))  # concurrent jobs add up
done <<< "$(sacct -u "$3" -S "$start_iso" -X -n -P -o Start,End,State 2>/dev/null)"
awk -v s="$sum" 'BEGIN { printf "%.3f\n", s/3600 }'
```

The unit is whatever your script reports; set `session_limit` and `--limit` in the same
unit (the bundled scripts report `jobh`). For a different metric — GPU-hours, core-hours,
node-hours — adapt the script and label `unit` accordingly. If the script exits non-zero,
`fail_mode` decides what happens (`closed` blocks the submission, `open` allows it).

Example of the guard in action:

```console
$ ctun -t horeka run -- sbatch big.sh
BLOCKED on horeka: session budget exhausted: 503.2 >= 500.0 jobh. Command not submitted.
```

---

## Using ctun with coding agents

`ctun` exists so coding agents can drive HPC clusters autonomously. The pattern:

- **A human logs in once.** Run `ctun -t <cluster> login` yourself, or have the agent call
  `ctun -t <cluster> login --interactive`, which pops a window for a present human to type
  the password — the agent never sees the secret.
- **The agent then prefixes every cluster command** with `ctun -t <cluster> run -- …`. No
  re-authentication is needed for the life of the tunnel, and the commands compose with
  normal shell scripting and pipes.
- **The budget guard is the safety net.** Even if the agent tries to submit more work, job
  submissions are blocked once the session limit is reached. `run` also **fails closed**:
  if the tunnel has dropped, the agent gets a clear error and must request a fresh `login`
  rather than anything proceeding unauthenticated.
- **`ctun -t <cluster> info`** gives the agent a concise briefing — the cluster
  description, advisory restrictions, and remaining budget — to read before it starts.

All agents must run `ctun` on the **same machine** where the tunnel was opened, since the
control socket is local to that machine.

---

## Files and locations

| Path | Purpose |
|---|---|
| `~/.config/cluster-tunnel/config.yaml` | Your configuration. |
| `~/.config/cluster-tunnel/budget/<cluster>.sh` | Per-cluster budget scripts. |
| `~/.cache/cluster-tunnel/sockets/<cluster>` | SSH control sockets (the live tunnels). |
| `~/.cache/cluster-tunnel/sessions/<cluster>.json` | Internal session state (start time, limit). |

---

*Design notes: see [PLAN.md](./PLAN.md) for the motivation and [DESIGN.md](./DESIGN.md)
for the detailed technical specification.*
