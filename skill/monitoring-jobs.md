# Monitoring a running Slurm job

How to keep a **live, updating view** of a Slurm job you submitted through `ctun`
— so you find out *the moment* it changes state or fails, without burning turns
polling `squeue` by hand. For the command syntax see [reference.md](reference.md);
for what may run where, see [cluster-etiquette.md](cluster-etiquette.md).

This uses your **`Monitor`** tool: it runs a command whose every stdout line
becomes a notification that wakes you. You arm it once, then do other work (or
idle) and react when an event lands. The command runs *on the login node* over
the live tunnel via `ctun run`, so the polling stays remote and cheap.

## The one rule that matters: silence is not success

A monitor that only watches for the *success* signal stays **silent** through a
crash, an OOM kill, a timeout, or a preemption — and silence looks **identical**
to "still running." So never watch only for the happy path. Either:

- track the Slurm **state** and emit on **every terminal state**, or
- filter the job's log for **failure signatures as well as progress**.

Before arming any monitor, ask: *if this job died right now, would my filter
emit anything?* If not, widen it.

Slurm terminal states to treat as "done" (success **or** failure):
`COMPLETED  FAILED  CANCELLED  TIMEOUT  OUT_OF_MEMORY  NODE_FAIL  BOOT_FAIL  DEADLINE  PREEMPTED`.

## Step 0 — capture the job ID (and log path) at submit

Submit with `--parsable` so you get just the numeric ID back (the budget guard
still applies — `sbatch` is guarded):

```
JOBID=$(ctun -t <cluster> run -- sbatch --parsable train.sh)
# the job's stdout/stderr log path (resolves %j/%x placeholders):
ctun -t <cluster> run -- scontrol show job "$JOBID" | grep -oP 'StdOut=\K\S+'
```

## Monitor A — Slurm state transitions (the authoritative "is it done?")

This is the one that reliably tells you when the job finishes, however it ends.
Arm it **persistent** so it survives a multi-hour job; the loop `break`s on any
terminal state, so the monitor ends itself the moment the job is done.

Call your `Monitor` tool with `persistent: true`, a specific `description`
(e.g. `"kcist job 537 state"`), and this command:

```bash
ctun -t <cluster> run -- bash -c '
  prev=""
  while true; do
    st=$(sacct -j '"$JOBID"' -n -X -o State 2>/dev/null | head -1 | tr -d " ") || true
    if [ -n "$st" ] && [ "$st" != "$prev" ]; then echo "job '"$JOBID"': $st"; prev="$st"; fi
    case "$st" in
      COMPLETED|FAILED|CANCELLED*|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL|BOOT_FAIL|DEADLINE|PREEMPTED)
        echo "job '"$JOBID"': TERMINAL ($st)"; break ;;
    esac
    sleep 30
  done'
```

You get one notification per state change (`PENDING → RUNNING → COMPLETED/…`)
and a final `TERMINAL (...)` line. On a terminal state, follow up with
`ctun -t <cluster> run -- sacct -j "$JOBID" -X -o State,ExitCode,Elapsed` for the
exit code.

## Monitor B — live log progress + errors (optional companion)

Run this *alongside* Monitor A to see application-level progress and catch
failures in the log. It must filter for **both** progress **and** error
patterns, and every pipe stage must be line-buffered or events sit unseen.

`Monitor` (`persistent: true`, description `"kcist job 537 log"`):

```bash
ctun -t <cluster> run -- tail -n +1 -f "<logpath>" \
  | grep -E --line-buffered \
      'loss=|epoch|step |Traceback|Error|Exception|srun:|slurmstepd:|CANCELLED|OOM|Killed|FAILED'
```

Notes:
- `tail -f` never exits on its own, so this monitor does **not** self-terminate.
  Stop it with **`TaskStop`** once Monitor A reports a terminal state (or when
  you no longer need progress).
- If the job writes errors to a separate file (`#SBATCH --error=...`), tail that
  one too, or rely on Monitor A for the failure verdict.
- Tune the progress tokens (`loss=`, `epoch`, …) to the job's actual output.

## Hygiene (applies to both)

- **Cadence:** poll no faster than ~30 s for a remote loop — the login node is
  shared. Local-only checks can go faster, but here everything is remote.
- **Resilience:** keep the `|| true` after `sacct` so one dropped query doesn't
  kill the whole monitor.
- **Line buffering:** any `grep`/`awk` in a pipe needs `--line-buffered` /
  `fflush()`, or notifications lag by minutes. Never pipe a raw, unfiltered log.
- **Selectivity:** emit only lines you'd act on (state changes, progress
  milestones, failure signatures). A firehose gets the monitor auto-stopped.

## If a monitor stops without a terminal state

The monitor's command runs through `ctun run`, so a **ctun-level** failure ends
the watch with that exit code, distinct from the job's own outcome:

- exit **10** (`ctun-error: login_required`) — the tunnel dropped. Ask the user
  to `ctun -t <cluster> login --interactive`, then re-arm the monitor.
- exit **11** (`budget_exhausted`) / **12** (`budget_guard_error`) — only the
  initial `sbatch` is budget-guarded, not the monitor loop; if you see these,
  it's the submission that was blocked. Stop and tell the user (never retry).

So: a monitor that ends on a `TERMINAL (...)` line means the **job** finished; a
monitor that ends *without* one means the **tunnel/connection** dropped — handle
them differently.

## Cleanup

Persistent monitors live until the session ends. When you're done with a job (or
abandon it), stop any still-running monitors with **`TaskStop`** so they don't
accumulate.
