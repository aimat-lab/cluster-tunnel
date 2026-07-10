---
name: cluster-tunnel
description: >-
  Drives OTP-protected HPC clusters through persistent authenticated SSH tunnels
  using the `ctun` CLI. Use when the user wants to run jobs, submit or monitor
  Slurm batch scripts, check the queue, install software, move files, or
  otherwise work on a remote cluster (e.g. haicore, horeka) — especially when
  the cluster login requires a one-time password (OTP). Covers checking tunnel
  status, logging in (including the interactive popup so a present human types
  the password/OTP), running commands through the live tunnel, the per-session
  compute-budget guard, and HPC etiquette (never run compute on login nodes;
  always go through Slurm).
allowed-tools: Bash(ctun *), Bash(uv run ctun *)
---

# Working on HPC clusters with `ctun`

`ctun` ("cluster-tunnel") keeps an authenticated SSH connection to an
OTP-protected HPC cluster alive in the background. A human logs in **once**;
after that every command you send reuses the live connection with **no
re-authentication**, and any Slurm job submission is checked against a
per-session compute budget before it reaches the scheduler.

Your job is to drive clusters through this CLI on the user's behalf. You never
see or handle passwords or one-time passcodes — a present human enters those.

## The one command shape to remember

```
ctun -t <cluster> <subcommand> [...]          # -t/--target ALWAYS goes BEFORE the subcommand
ctun -t haicore run -- squeue --me            # everything after `--` runs verbatim on the cluster
```

> If `ctun` is not on the PATH, you are in the development repo: use
> `uv run ctun ...` instead (run it from the `cluster-tunnel` project root).

## Live tunnel status (injected now)

```!
ctun status 2>/dev/null || uv run ctun status 2>/dev/null || echo "(ctun unavailable — install it or run 'uv run ctun' from the cluster-tunnel repo)"
```

## Startup workflow — run this before working with any cluster

1. **Check status first.** Read the status snapshot above (or run
   `ctun status`). It lists every configured cluster and whether its tunnel is
   `live` or `down`. Never assume a tunnel is up.

2. **Read the briefing before you touch a cluster — every cluster, every
   session.** The first time you work with a given cluster, and before any
   `login` or `run` against it, run `ctun -t <cluster> info` and read it. This
   is how that cluster's specific context reaches you: it prints the cluster
   **description**, advisory restrictions (partitions, max runtime, GPU caps),
   and the compute budget (limit, unit, which commands are guarded). This is not
   optional and not just for job submission — do it even for read-only work like
   `squeue`, and even if the tunnel is already `live`. If you switch to a second
   cluster later in the session, run its `info` before touching it too.

3. **Bring up the tunnels you need via interactive login.** For any cluster
   that is `down` and that the task needs, run:

   ```
   ctun -t <cluster> login --interactive
   ```

   This pops up a small window on the user's screen for a present human to type
   the password and OTP — **you never see the secret**. If the user has asked
   you to prepare every cluster, do this once per `down` cluster so they get one
   login window each. Tell the user a window has appeared and to fill it in.

   - Use plain `ctun -t <cluster> login` (terminal prompt) only if the user
     explicitly says they'll log in at the terminal themselves.
   - A login lasts until the tunnel drops or someone runs `logout`; it also
     starts a fresh compute-budget session.

4. **From here, use the cluster freely** per the user's instructions: submit
   and monitor jobs, inspect results, install dependencies, move data — all via
   `ctun -t <cluster> run -- ...`.

If a `run` fails with "No live tunnel", the connection dropped: ask the user to
log in again (step 3). Never try to work around a missing tunnel.

## Golden rules on the cluster

These are not optional — violating them harms a shared academic machine. See
[cluster-etiquette.md](cluster-etiquette.md) for the full reasoning, Slurm
patterns, and the budget guard explained.

- **Read `ctun -t <cluster> info` before your first action on a cluster.** Its
  description and restrictions are the cluster's specific context — you cannot
  work correctly without them. See the startup workflow above.
- **Never run real compute on a login node.** Anything using real CPU/GPU/RAM
  or running more than a few seconds **must** go through Slurm (`sbatch`,
  `srun`, `salloc`). No `python train.py` on the login node. The explicit
  exception is **installing software / building environments** (`pip install`,
  `conda`/`mamba`, `module load`, compiling) — that belongs on the login node.
- **Respect the budget guard.** When a job submission is blocked because the
  session budget is spent, **stop and tell the user** — never retry, split, or
  bypass it.
- **Respect the advisory restrictions** from `ctun -t <cluster> info` (allowed
  partitions, max runtime, max GPUs); they are not auto-enforced.
- **Be a good tenant.** Request only what a job needs, set realistic `--time`
  limits, and prefer batch jobs over idle interactive allocations.

## Quick command recipes

```
ctun status                              # all clusters: live/down, budget, activity
ctun -t haicore info                     # briefing: description, restrictions, budget
ctun -t haicore login --interactive      # human enters password + OTP in a popup
ctun -t haicore run -- squeue --me       # check your queue (safe on login node)
ctun -t haicore run -- sbatch train.sh   # submit a batch job (budget-checked)
ctun -t haicore run -- sacct -j 12345    # inspect a finished/running job
ctun -t haicore run -n -- sbatch big.sh  # -n/--dry-run: show the budget decision, don't submit
ctun -t haicore upload ./data $WORK/data # copy local -> cluster (rsync, no re-auth)
ctun -t haicore download out/run.log .   # copy cluster -> local
ctun -t haicore logs                     # commands sent to this cluster this session
ctun -t haicore logout                   # close the tunnel, clear the session
```

Tips:
- Everything after `--` is forwarded verbatim, so flags like `--me` or
  `--time=01:00:00` pass straight through. Use a remote shell for chained work:
  `ctun -t haicore run -- bash -c 'cd $WORK && sbatch job.sh'`.
- The command's exit code becomes `ctun`'s exit code, and output streams live.
- Transferring many small files? Pack them into one `tar.gz`, transfer that,
  and unpack on the other side (via `ctun run -- tar xzf …` for uploads) — far
  faster and gentler on the shared filesystem. Details in
  [reference.md](reference.md).
- Add `--tty` for interactive remote programs that need a pseudo-terminal.
- After submitting a job, don't poll `squeue` by hand — set up a live monitor
  that wakes you on state changes and failures: see
  [monitoring-jobs.md](monitoring-jobs.md).

## More detail

- **Full CLI reference** (every command, flag, and JSON output): see
  [reference.md](reference.md).
- **HPC etiquette, Slurm patterns, and the budget guard explained**: see
  [cluster-etiquette.md](cluster-etiquette.md).
- **Live-monitoring a running job** (get notified the moment it changes state
  or fails, via your `Monitor` tool over the tunnel): see
  [monitoring-jobs.md](monitoring-jobs.md).
- **Python projects on the cluster** (optional; always use venvs, prefer `uv`,
  where to put the env, activating it in sbatch scripts): see
  [python-projects.md](python-projects.md).
- **How to install this skill** into a project or your personal skills: see
  [README.md](README.md).
