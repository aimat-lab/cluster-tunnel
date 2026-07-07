# HPC cluster etiquette, Slurm, and the budget guard

Behavioral guidance for working on shared academic HPC clusters through `ctun`.
Load this when planning real work on a cluster (submitting jobs, building
environments, managing data). For the command syntax, see [reference.md](reference.md).

## Contents

- The login node is not a workstation
- Submitting work through Slurm
- Be a considerate tenant
- The compute-budget guard
- When the tunnel drops

## The login node is not a workstation

Every command you send with `ctun -t <cluster> run -- ...` lands on a **shared
login node** used by many people at once. Treat it as a thin entry point, not a
place to compute.

**Fine on the login node** (cheap, short, interactive):
- Inspecting the queue and accounting: `squeue --me`, `sacct`, `sinfo`.
- File and code management: `ls`, `cd`, `cat`, `grep`, editing, `git`, `rsync`
  of modest files, creating/cleaning directories.
- **Building environments and installing software** — the explicit exception:
  `module load ...`, `pip install ...`, `conda`/`mamba` env creation,
  compiling. This is expected to happen on the login node (or a dedicated build
  step), not inside a job.

**Never on the login node** (must go through Slurm):
- Training, inference, simulation, data processing, anything GPU/heavy-CPU.
- Long-running or memory-hungry processes.
- "Just a quick test" of a training script — use an interactive Slurm session
  instead (`srun`/`salloc`).

If you're unsure whether something belongs in a job: if it uses real compute or
runs more than a few seconds, it goes through Slurm.

## Submitting work through Slurm

**Batch jobs** (the default — preferred over holding interactive sessions):

```
ctun -t <cluster> run -- sbatch train.sh
```

A typical batch script declares its resources up front:

```bash
#!/bin/bash
#SBATCH --job-name=train
#SBATCH --partition=accelerated     # use a partition allowed by `ctun info`
#SBATCH --gres=gpu:1                # request only what you need
#SBATCH --time=02:00:00             # realistic wall-clock limit
#SBATCH --signal=TERM@120           # SIGTERM ~120s before the limit — time to checkpoint/clean up
#SBATCH --output=logs/%x-%j.out
srun python train.py
```

**Interactive allocations** (for debugging on a compute node — release promptly):

```
ctun -t <cluster> run --tty -- srun --partition=accelerated --gres=gpu:1 --time=00:30:00 --pty bash
```

**Monitoring:**

```
ctun -t <cluster> run -- squeue --me
ctun -t <cluster> run -- sacct -j <jobid> --format=JobID,State,Elapsed,MaxRSS
ctun -t <cluster> run -- scancel --full --signal=TERM <jobid>   # graceful cancel — SIGTERM, let it clean up
```

## Be a considerate tenant

- **Prefer batch over idle interactive sessions.** Don't hold a GPU allocation
  open while thinking; submit a batch job or release the allocation.
- **Use scratch/work areas correctly** and clean up large outputs you no longer
  need. Don't fill shared storage.
- **Look before you delete a directory.** Removing one file is low-risk;
  `rm -rf` on a *directory* — or "cleaning up" a scratch/output area — is not.
  `ls` the target first and confirm everything in it is yours and meant to go:
  shared scratch and run-output dirs routinely hold other people's data, or an
  earlier run's results you didn't create. When unsure, delete the specific
  files you made, never the enclosing directory.
- **Terminate jobs gracefully — don't hard-kill them.** Prefer a soft signal
  (`scancel --full --signal=TERM <jobid>`) and give jobs lead time to shut down
  (`#SBATCH --signal=TERM@<seconds>`). A hard SIGKILL on a job doing
  multiprocessing, distributed training, or heavy I/O can orphan child processes
  or leave cleanup half-done, which can **drain the node** for everyone. Set the
  lead time to cover your job's cleanup/checkpoint needs — the default ~30s is
  often too short.
- **Honor the advisory restrictions** from `ctun -t <cluster> info` (allowed
  partitions, max runtime, max GPUs). `ctun` does not enforce these — you do.

## The compute-budget guard

`ctun` enforces one hard limit: a **per-session compute budget**. A "session"
is the period since the last `login`.

How it works:
1. Before running a command whose first word is a **guarded command**
   (`sbatch`, `srun`, `salloc`, plus any extras shown by `info`), `ctun` runs
   the cluster's budget script on the login node over the tunnel.
2. That script prints a single number — compute used since the session started.
3. If `used >= limit`, the submission is **blocked** before it reaches the
   scheduler. Otherwise it runs, and remaining budget is shown on stderr.

Non-submission commands (`squeue`, `sacct`, `ls`, ...) always pass through. If
no limit is set, the cluster runs unguarded.

**When a submission is blocked:**
- **Stop. Report it to the user.** Do not retry the same submission, do not
  split it into smaller jobs to sneak under the guard, do not try to disable or
  bypass the guard. It exists to protect the user's allocation.
- The user can free budget by cancelling queued jobs, or start a fresh session
  with a new `login` (which resets the window).
- Use `ctun -t <cluster> run -n -- sbatch job.sh` (`--dry-run`) to preview
  whether a submission would be allowed **before** committing to it.

**fail_mode.** If the budget script itself errors, `fail_mode: closed` blocks
the submission (safe default) and `fail_mode: open` allows it. A
`closed`-blocked submission means the *probe* is broken — report that the budget
can't be measured, rather than assuming the job is at fault.

## When the tunnel drops

If a `run` fails with "No live tunnel", the authenticated connection ended
(network change, idle timeout, or the cluster's session policy). `ctun` fails
closed: nothing proceeds unauthenticated. Ask the user to
`ctun -t <cluster> login --interactive` again — a fresh login also starts a new
budget session.
