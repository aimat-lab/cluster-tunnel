# `ctun` CLI reference

Full reference for driving clusters with `ctun`. Load this when you need the
exact flags, behaviors, or machine-readable output. For the day-to-day
workflow and golden rules, see [SKILL.md](SKILL.md).

## Contents

- Invocation shape and global options
- `status` — tunnel + session state
- `jobs` — Slurm job overview
- `info` — cluster briefing
- `login` — authenticate and open the tunnel
- `run` — run a command through the tunnel
- `upload` / `download` — transfer files over the tunnel
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

## `jobs` — Slurm job overview

```
ctun jobs                    # all clusters with a live tunnel
ctun -t <cluster> jobs       # one cluster
ctun jobs --since 6h         # widen/narrow the finished-job window (default 24h)
ctun jobs --since 0          # live queue only (no finished jobs)
ctun -t <cluster> jobs -j    # machine-readable JSON
```

A quick overview of **your** jobs — running and pending (from `squeue`), plus
jobs that **finished recently** (from `sacct`, within `-s/--since`, default
`24h`; `--since 0` skips them). Without `-t` it sweeps every configured cluster
and reports each one that has a live tunnel — down clusters are noted, never
logged in to. Each cluster gets its own table: job ID, name, state, elapsed,
time limit, partition, nodes — active jobs first, then finished newest-first.

This is read-only (a bundled probe runs `squeue`/`sacct` on the login node over
the tunnel), so it is **never budget-guarded**. It is the fast way to answer
"what's running / what just happened" across clusters; for a live, updating view
of a *single* job as it changes state, use `Monitor` (see
[monitoring-jobs.md](monitoring-jobs.md)). Equivalent to
`run -- squeue --me` + `run -- sacct`, but formatted and multi-cluster.

## `info` — cluster briefing

```
ctun -t <cluster> info       # one cluster
ctun info                    # every configured cluster
ctun -t <cluster> info -j    # JSON
```

Prints the agent briefing: description, connection details (host, user, whether
a **password** and/or **OTP** is required), advisory **restrictions** (partitions, runtime, GPU
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

## `upload` / `download` — transfer files over the tunnel

```
ctun -t <cluster> upload [-n|--dry-run] <local-src> <remote-dest> [-- <rsync args>]
ctun -t <cluster> download [-n|--dry-run] <remote-src> <local-dest> [-- <rsync args>]
```

Move files with **rsync over the live tunnel** — it reuses the authenticated
master, so there is **no re-authentication** (no password, no OTP).

- `upload` copies local → cluster; `download` copies cluster → local. The
  direction decides which side is remote, so remote paths are written **bare**
  (no `host:` prefix); a relative remote path resolves to the remote `$HOME`.
- Copies **recursively** (`rsync -r`). Trailing slashes follow rsync semantics:
  `data/` copies the *contents* of `data`, `data` copies the directory itself.
- **Not** budget-guarded (moving data isn't compute). **Fails closed**: with no
  live tunnel it exits `10` (`ctun-error: login_required`) instead of prompting.
- rsync's exit code is propagated. Extra rsync flags go after `--`, e.g.
  `-- --exclude='*.tmp' -z`, or `-- --info=progress2` for a progress display.

```
ctun -t haicore upload ./dataset $WORK/dataset       # push a folder
ctun -t haicore download logs/run42.out ./run42.out  # pull a result file
```

### Best practice: archive many small files before transferring

Transferring a tree with many small files (datasets of individual samples,
Python envs, checkpoints with shards) is slow per-file and hammers the
cluster's shared filesystem. Bundle it into **one archive**, transfer that, and
unpack on the other side. Prefer **tar.gz** over zip: it preserves Unix
permissions and symlinks, and `tar` is always available on clusters (`unzip`
often isn't).

Local → cluster:

```
tar czf dataset.tar.gz dataset/                                  # pack locally
ctun -t haicore upload dataset.tar.gz $WORK/dataset.tar.gz       # one big transfer
ctun -t haicore run -- bash -c 'cd $WORK && tar xzf dataset.tar.gz && rm dataset.tar.gz'
```

Cluster → local:

```
ctun -t haicore run -- bash -c 'cd $WORK && tar czf results.tar.gz results/'
ctun -t haicore download $WORK/results.tar.gz ./results.tar.gz
tar xzf results.tar.gz                                           # unpack locally
```

- Unpack **on the login node is fine** — `tar` is I/O, not compute — but for
  archives with very many files prefer unpacking into `$WORK`/scratch rather
  than `$HOME`, which often has tight file-count (inode) quotas.
- If the user insists on zip, check `unzip` exists first
  (`ctun -t <cluster> run -- command -v unzip`).
- For a **single large file** or for **re-syncing** a tree that already exists
  on the other side, skip the archive — plain `upload`/`download` (rsync)
  transfers deltas only.

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
    requires_password: true     # optional (default true); false = OTP/key only, no password
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
