# `ctun` CLI reference

Full reference for driving clusters with `ctun`. Load this when you need the
exact flags, behaviors, or machine-readable output. For the day-to-day
workflow and golden rules, see [SKILL.md](SKILL.md).

## Contents

- Invocation shape and global options
- `status` — tunnel + session state
- `info` — cluster briefing
- `login` — authenticate and open the tunnel
- `run` — run a command through the tunnel
- `logs` — commands sent this session
- `logout` — close the tunnel
- `config` — manage the config file
- JSON output for scripting
- Failure modes and what they mean

## Invocation shape and global options

The target cluster is selected with the global `-t/--target` option, placed
**before** the subcommand:

```
ctun -t <cluster> <subcommand> [options]
```

| Global option | Meaning |
|---|---|
| `-t, --target <name>` | Cluster to act on (a key under `clusters:` in the config). |
| `-c, --config <path>` | Use an alternate config file (overrides `$CTUN_CONFIG`). |
| `-V, --verbose` | Enable debug logging. |
| `--version` | Print the version. |

`<cluster>` is a name from the config (e.g. `haicore`, `horeka`, `localhost`).
Run `ctun status` to see the configured names.

## `status` — tunnel + session state

```
ctun status                 # all configured clusters
ctun -t <cluster> status    # one cluster
ctun -t <cluster> status -j # machine-readable JSON
```

Columns: cluster, tunnel `live`/`down`, session start time, budget limit,
budget **used** (only probed for an explicitly targeted, live, guarded
cluster — never for the all-clusters overview, to avoid slow remote probes),
command count, and the time of the last command. Use this first whenever you
lack context.

## `info` — cluster briefing

```
ctun -t <cluster> info       # one cluster
ctun info                    # every configured cluster
ctun -t <cluster> info -j    # JSON
```

Prints the agent briefing: description, connection details (host, user, whether
**OTP** is required), advisory **restrictions** (partitions, runtime, GPU
caps), and the **budget** (used vs limit, unit, the guarded commands, the
fail-mode, and the path of the budget script). Works even without a live
tunnel. Read this before submitting jobs.

## `login` — authenticate and open the tunnel

```
ctun -t <cluster> login [-i|--interactive] [-l|--limit N] [--timeout S] [-v]
```

- Opens the persistent SSH master connection. This is the only step needing the
  password + OTP.
- `-i, --interactive`: show a popup window (password + budget limit) so a
  present human enters the credentials; the secret is fed to ssh directly and
  **never exposed to the agent**. Use this whenever you (an agent) trigger a
  login. `--timeout S` bounds the wait (default 120s).
- `-l, --limit N`: this session's compute budget, overriding the cluster's
  configured `session_limit`. `-1` means infinite / unguarded.
- `-v` (repeatable `-vv`/`-vvv`): surface ssh's own diagnostics on failure.

A **session** lasts for one authentication. Logging in again (after a drop)
starts a fresh session and resets the budget window. On success `ctun` prints a
panel with the target and the session's budget.

## `run` — run a command through the tunnel

```
ctun -t <cluster> run [--tty] [-n|--dry-run] -- <command> [args...]
```

- Everything after `--` is forwarded to the cluster **exactly as written**.
- The remote command's **exit code is propagated** as `ctun`'s exit code;
  output streams live.
- `--tty`: allocate a pseudo-terminal for interactive remote programs.
- `-n, --dry-run`: report the budget decision (ALLOW/BLOCK + reason) without
  executing. Useful to preview whether a submission would be allowed.
- If there is **no live tunnel**, `run` fails immediately telling you to
  `login` — it never silently prompts for a password.

When a guarded command runs and a limit is set, `ctun` prints the remaining
budget to stderr before the command's own output, so it never pollutes stdout.

```
ctun -t haicore run -- squeue --me
ctun -t haicore run -- bash -c 'cd $WORK && sbatch job.sh'
```

## `logs` — commands sent this session

```
ctun -t <cluster> logs        # requires -t
ctun -t <cluster> logs -j     # JSON
```

Prints the timestamped list of commands sent to the cluster during the current
session. **Only the commands are recorded — never their output.** The log is
temporary: cleared on `logout` and on a fresh `login`. Requires a target.

## `logout` — close the tunnel

```
ctun -t <cluster> logout
```

Tears down the master connection and clears the session record and command log.

## `config` — manage the config file

```
ctun config            # open in $EDITOR
ctun config -i|--init  # create a starter config if none exists
ctun config -p|--path  # print the config file path
ctun config -s|--show  # print the resolved config
ctun config --validate # check the config for errors
```

A cluster entry looks like:

```yaml
clusters:
  haicore:
    host: haicore.scc.kit.edu
    user: ab1234
    requires_otp: true
    description: |
      HAICORE @ KIT. GPU jobs via Slurm.
    restrictions:               # advisory only — shown by `info`, never enforced
      allowed_partitions: [accelerated]
      max_gpus_per_job: 4
    budget:                     # omit to run unguarded
      script: budget/haicore.sh
      unit: jobh                # job-hours: Slurm job wall-clock since session start
      guard_commands: [sbatch, srun, salloc]
      fail_mode: closed         # if the budget script errors: closed blocks, open allows
```

Editing config is a user action — don't change a user's cluster definitions
unless asked.

## JSON output for scripting

`status`, `info`, and `logs` accept `-j/--json` for stable machine-readable
output. Prefer these when you need to parse state programmatically rather than
scraping the rendered tables/panels.

## Failure modes and what they mean

| Symptom | Meaning | What to do |
|---|---|---|
| `No live tunnel for '<cluster>'` | The tunnel dropped or was never opened. | Ask the user to `login --interactive`. Don't work around it. |
| `Submission blocked … budget exhausted` | The session's compute budget is spent. | Stop. Tell the user. Don't retry or bypass. |
| `Submission blocked … fail_mode closed` | The budget script errored and the cluster fails closed. | Report it; the budget probe is broken, not the job. |
| Interactive login `failed or timed out` | Nobody filled the popup, or auth failed. | Ask the user to retry; add `-v` for ssh diagnostics. |
| `No display for the login dialog` | No GUI available for the popup. | Ask the user to run plain `ctun -t <cluster> login` in a terminal. |
