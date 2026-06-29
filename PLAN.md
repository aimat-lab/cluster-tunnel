# cluster-tunnel (`ctun`) — Project Plan

> This document covers *the problem and the high-level plan*. For the concrete
> technical specification (command details, SSH mechanics, session/budget flow,
> config schema, implementation layout), see [DESIGN.md](./DESIGN.md).

## 1. The problem

We want **coding agents to operate autonomously on OTP-protected HPC clusters**
(KIT's bwUniCluster / HoreKa / HAICORE, JSC's JUWELS, intnano, …), but two things
make that hard today:

1. **One-time-password (OTP / 2FA) auth on every SSH connection.**
   If an agent opens a fresh SSH connection per command, a human has to type a
   password + OTP token *every single time*. That makes unattended, multi-step
   agent work over SSH effectively impossible.

2. **No guardrails on compute spend.**
   An agent left to submit jobs can burn through a finite compute-time allocation
   (core-/node-hours) with no awareness of the budget. We need a hard limiter that
   sits between the agent and the scheduler.

## 2. The idea

A small command-line tool — **`ctun`** (short for *cluster-tunnel*) — installed on
`euler` (the always-on controller node where the agents run). It acts as a **single
authenticated doorway** to each cluster:

```
ctun -t horeka run -- squeue --me
ctun --target=horeka run -- sbatch train.sh
```

A human authenticates **once** per cluster; afterwards every agent invocation
reuses that live connection with **zero re-authentication**, and every
job-submitting command is checked against the **current session's** compute
budget before it is allowed through.

Because `ctun` is just a CLI *prefix*, it composes naturally with normal bash and
shell scripting — no special agent integration (e.g. MCP server) required.

## 3. How it works

### 3.1 Persistent auth — SSH connection multiplexing

Built on OpenSSH **`ControlMaster` / `ControlPersist`**:

- A one-time `login` opens a **master connection** and leaves a **control socket**
  alive in the background (e.g. `~/.cache/cluster-tunnel/sockets/<cluster>`).
- Every later `ctun … run` runs `ssh -S <socket> <host> "<cmd>"`, **reusing** the
  master — so no password/OTP is needed again until the socket expires or is closed.

**Key consequence:** the control socket is **local to the machine running `ctun`**.
For "authenticate once, shared by all agents" to hold, **all agents must invoke
`ctun` on the same `euler` box**. (Confirmed for our setup.)

Each command starts a fresh remote shell (no persistent cwd/env between commands) —
acceptable for the agent-funneling use case; state is carried in the commands
themselves or via job scripts.

### 3.2 Login flows

- **`ctun -t <cluster> login`** — for a human at the terminal: runs interactive SSH
  in the current TTY; the human types password + OTP; the master socket is
  established.
- **`ctun -t <cluster> login --interactive`** — **agent-triggerable**: pops a small
  **tkinter dialog** (password/OTP + session limit) in which the **present human**
  authenticates, then drives the SSH master inside a **pseudo-terminal**, typing the
  password in; the master backgrounds (`-f`) and persists. Meanwhile `ctun` blocks,
  polling `ssh -O check` until the socket is live, then returns success. The agent
  never sees the secret — it just *summons* the human and waits. (Routing the master
  through a terminal window proved fragile; see [DESIGN.md §3.1](./DESIGN.md).)
- When a `run` is attempted with **no live tunnel**, it **fails loudly** with a clear
  message telling the agent to request a `login` (it does **not** silently prompt,
  since no human may be present).

### 3.3 Compute-budget guard — *session-scoped threshold block*

The guard limits **how much compute the current session has consumed**, not how
much of the total allocation remains. A **session = the lifetime of one
authentication** (per cluster): it begins when OTP is entered at `login`, and is
replaced only when OTP is entered again (re-auth after the tunnel died). Usage
accumulates over that whole window and zeroes only on re-auth — there is no manual
reset, so a fresh budget always requires a human at the OTP prompt.

Before executing a **submission command** (`sbatch` / `srun` / `salloc`), `ctun`:

1. Reads the session's **start timestamp** from its internal cache.
2. Runs a per-cluster **bash script** *on the login node* (piped over the tunnel),
   passing the start time as an argument. The script — which owns all the
   cluster-specific accounting — prints **one number**: compute used since the
   session started.
3. Compares it to the **session limit** (`login --limit`, else config
   `session_limit`). If **used ≥ limit**, the submission is **hard-blocked** and
   never reaches the scheduler. If the script errors, the configurable `fail_mode`
   decides (default **closed** = block).

This is a **threshold** gate, *not* per-request cost prediction — we do not parse a
job's requested nodes×walltime to forecast its individual cost. Non-submission
commands (`ls`, `squeue`, `sacct`, …) always pass through unguarded, and a cluster
with no limit configured runs entirely unguarded.

### 3.4 Configuration

Per-user config at `~/.config/cluster-tunnel/config.yaml` (override with
`$CTUN_CONFIG` / `--config`), reusing existing `~/.ssh/config` aliases where they
already exist (`bwuni`, `haicore`). Each cluster entry defines:

- SSH `host` / `user` / `identity_file` (or an `ssh_alias`), and `ControlPersist`
  TTL.
- An optional human-readable `description` and **advisory** `restrictions` (max
  runtime, GPUs, partitions, …) — surfaced to the agent via `info`, never enforced.
- An optional **`budget`** block: the **script** to run (default
  `budget/<cluster>.sh`), the default **`session_limit`** + **unit**, which
  `guard_commands` count as submissions, and the **`fail_mode`**.

A cluster with **no `budget` block** (or no limit) simply tunnels + runs with **no
enforcement**. A global `agent.preamble` is prepended to every cluster's `info`
briefing. See [DESIGN.md §6](./DESIGN.md) for the full annotated schema.

## 4. Decisions locked in

| Topic | Decision |
|---|---|
| Form factor | **CLI prefix tool** (`ctun`), *not* an MCP server — composes with bash. |
| Persistent auth | OpenSSH **ControlMaster / ControlPersist** multiplexing. |
| CLI selector | Cluster as a flag: `ctun -t <cluster> …` / `--target=<cluster>`. |
| Auth entry | `login` (human) **and** `login --interactive` (agent → blocking popup for human). |
| Dead tunnel | `run` **fails closed**, prompting an agent to request re-login. |
| `run` syntax | `--`-only passthrough: `ctun -t <c> run -- <cmd…>` (no quoted form). |
| Session | = one authentication's lifetime per cluster; resets **only on re-auth**. |
| Budget enforcement | **Session-scoped threshold** block when `used ≥ limit`; fail closed on probe error. |
| Budget metric | per-cluster **remote bash script** prints compute used since session start. |
| Limit source | `login --limit`, else config `session_limit`, else unguarded. |
| Restrictions | **advisory only** (surfaced via `info`), never enforced. |
| Config | per-user **YAML** `~/.config/cluster-tunnel/config.yaml`. |
| Cache | **internal-only** session state; no general key–value store. |
| Stack | **Python + rich-click**, mixin-based CLI architecture, `uv`. |
| Host | Runs on **`euler`**; all agents call `ctun` here so they share one socket. |
| Target clusters | bwUniCluster, HoreKa, JUWELS, HAICORE / intnano. |

## 5. Planned command surface (initial)

- `ctun -t <cluster> login [--interactive] [--limit N]` — authenticate (OTP) and open
  the tunnel; starts the session and records its budget limit.
- `ctun -t <cluster> run -- <cmd…>` — run a command through the tunnel (budget-guarded).
- `ctun -t <cluster> status` — tunnel live? show socket / session usage vs limit.
- `ctun -t <cluster> info` — agent briefing: description + advisory restrictions + budget.
- `ctun -t <cluster> logout` — tear down the master socket and clear the session.
- `ctun config` — open the YAML config in `$EDITOR` (`--show` / `--path` / `--validate`).
- `ctun status` — overview of all configured clusters and tunnel states.
- `ctun webui` *(future)* — local Flask + JS interface.

## 6. Environment (verified on `euler`)

- Display `:1` (X11); `gnome-terminal`, `xfce4-terminal`, `x-terminal-emulator`,
  `zenity` available → blocking popup login is feasible.
- `uv` 0.10.4, Python 3.12; OpenSSH 9.6 (full ControlMaster support).
- Existing `~/.ssh/config` aliases: `bwuni` → `uc2.scc.kit.edu`, `haicore` →
  `haicore.scc.kit.edu` (KIT account `ab1234`).
- AutoSlurm configs (`/media/ssd2/Programming/AutoSlurm/auto_slurm/configs/`) provide
  partitions/accounts per cluster (e.g. JUWELS account `aimatchem`, HoreKa partition
  `accelerated`).

## 7. Open input still needed (to switch enforcement on)

The only thing that can't be inferred: the **per-cluster budget script**
(`budget/<cluster>.sh`). For each cluster we want guarded, we need a small bash
script that, given the session start time (`$1` = UTC epoch, `$2` = cluster,
`$3` = user) and running on the login node, prints **one number** — the compute
used since that time, in whatever unit we'll set the limit in. We need:

- the exact accounting command(s) for that cluster (e.g. an `sacct`/`sreport`
  invocation, or a site budget tool),
- a sample of their output (to write the script),
- the unit (core-hours / GPU-hours / node-hours / …) and a sensible default
  `session_limit`.

Until provided, those clusters tunnel + run **without** budget enforcement
(framework is built; enforcement is per-cluster config + script, switched on
incrementally). See [DESIGN.md §5.1](./DESIGN.md) for the script contract and an
illustrative template.
