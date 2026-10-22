# `ctun` CLI reference

Full reference for driving clusters with `ctun`. Load this when you need the
exact flags, behaviors, or machine-readable output. For the day-to-day
workflow and golden rules, see [SKILL.md](SKILL.md).

## Contents

- Invocation shape and global options
- `status` — tunnel + session state
- `jobs` — Slurm job overview
- `jobs edit` — change jobs' project or note
- `projects list` — every project in the job records
- `projects rename` — rename a project in every job record
- `info` — cluster briefing
- `note` — record a fact about a cluster (after the user approves)
- `login` — authenticate and open the tunnel
- `hostkey` — check (and, the user's call, pin) the server's host key
- `run` — run a command through the tunnel
- `upload` / `download` — transfer files over the tunnel
- `logs` — commands sent this session
- `logout` — close the tunnel
- `config` — manage the config file
- JSON output for scripting
- Failure modes and what they mean
- Windows (experimental)

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

`ctun jobs list` is the same command, named next to `jobs edit`.

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

With `--json`, each cluster entry names its `slurm_cluster`: Slurm's own
`ClusterName`, which jobs see as `SLURM_CLUSTER_NAME` (`null` when unknown or the
tunnel is down). Each job carries its `record` (see [Job records](#job-records), or
`null`; the tasks of an array share the array's record, unless `jobs edit` gave
a task its own), and each cluster entry lists `records_without_job`: records written or
changed in the window whose job is no longer listed. `records_error` reports a
failure to read the records; the jobs are still listed.

## `jobs edit` — change jobs' project or note

```
ctun -t <cluster> jobs edit <id> --project htfm/mh --note "…"   # change project and note
ctun -t <cluster> jobs edit <id> <id>... --project htfm/new     # the same change for several jobs
ctun -t <cluster> jobs edit <id> --project "" --note ""         # clear: Unassigned, no note
ctun -t <cluster> jobs edit <id> --confirm-note                 # the human confirmed the note
```

Changes each job's [record](#job-records) on the cluster, so every machine and
front end sees the change, and prints the resulting records as a JSON list. With
several jobs, all of them are checked first: an unknown job or reference changes
none of them. They are read and stored a few dozen per SSH call; running the
command again leaves the jobs that already have the change as they are. To read
records without changing them, use `jobs --json`; widen `--since` for older
jobs. A `/` in a project nests it. A new note is unconfirmed until
`--confirm-note`, and its references are checked as in `run`, commits in the
job's own repo too; an unknown one refuses the edit (exit code 13).
Each edit appends `{at, from, <field>: [old, new]}` to the record's `changes`;
the facts from submit time are never changed. A job that has no record yet
(e.g. one submitted with plain `sbatch`) gets one without facts; ctun refuses a
job that Slurm doesn't know. A task of an array (`jobs edit 123_4`) gets a copy
of the array's record to change, so it keeps the array's project and facts.

**Only change a job after the user's explicit yes** to the exact command.

## `projects list` — every project in the job records

```
ctun projects list                    # all clusters with a live tunnel
ctun -t <cluster> projects list -j    # one cluster, machine-readable JSON
```

A project exists only through the jobs whose [record](#job-records) names it, so
this reads every record on the cluster, however old, and counts the jobs per
project. Use it to reuse an existing project name instead of inventing a new
one. Jobs without a project are not counted. Read-only; down tunnels are
reported, never logged in to.

`--json` prints `{"clusters": [{cluster, live, job_records, error, projects}]}`,
where `projects` is `[{project, jobs}]` sorted by name. `job_records` is false
for a cluster configured without records, which is skipped (its `live` is
`null`: not checked). `error` is set when
the records could not be read.

## `projects rename` — rename a project in every job record

```
ctun projects rename htfm archive/htfm             # every cluster with a live tunnel
ctun -t <cluster> projects rename htfm eval -j     # one cluster, JSON
```

Gives every job of project OLD the project NEW, and the projects nested in OLD
along with it: `htfm/mh` becomes `archive/htfm/mh`. If NEW already exists, the
jobs join it. Both names lose a `/` or blanks at either end (`archive/` is
`archive`), as every project name does when it is stored. Each change is logged in the record's `changes`, as with
`jobs edit`. A down cluster keeps OLD; log in and run the command again, which
is safe to repeat, also when NEW lies inside OLD (`htfm` to `htfm/old`): the
projects already in NEW stay where they are.

`--json` prints `{"clusters": [{cluster, live, job_records, error, renamed}]}`,
where `renamed` lists the job IDs whose record changed, also when `error` says
the rename stopped partway; `job_records` and `error` are as in
`projects list`. A record edited on another machine meanwhile keeps that edit:
ctun reads it again and renames it from there.

It exits 1 when the rename failed on any cluster. With `-t <cluster>`, a down
tunnel exits 10 (`ctun-error: login_required`), and a cluster that keeps no
records exits 1. Without a target, down clusters are only noted. The records go back to the cluster a few
dozen per SSH call.

**Only rename a project after the user's explicit yes** to the exact command.

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

The description is either one block of text or a **list of notes**, shown as
bullet points. Either way it is the user's own context for that cluster: treat
the notes as standing instructions and facts. In `-j` output, `description` is
a string or a list of strings, matching the config.

## `note` — record a fact about a cluster

```
ctun -t <cluster> note "<text>"    # requires -t; quote the text
```

Appends `<text>` as one more note to the cluster's `description` list in the
user's config, with a `# added via ctun note, <date>` comment; `info` shows it
from then on. Only those lines of the config change. **Run it only after the
user has approved the exact text** (see [SKILL.md](SKILL.md)). It refuses when
the cluster's description is still one string: say so and leave
`ctun config --migrate` to the user.

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

## `hostkey` — check and pin the server's host key

```
ctun -t <cluster> hostkey [-j|--json]
ctun -t <cluster> hostkey --pin [-y|--yes]
```

- Shows the host key the cluster's server presents and what a login would make
  of it (a key it trusts, a new one, or a different one). It connects without
  logging in: no password, no OTP, no tunnel needed.
- Until a key is pinned, logins accept the key on first contact (`accept-new`).
- `--pin` stores the key in a known_hosts file next to the config and sets
  `strict_host_key_checking: 'yes'` and `known_hosts_file` in the cluster's
  config entry, so later logins refuse any other key. It shows the change and
  asks; without a terminal it only shows it, unless `-y/--yes`.
- **Pinning is the user's decision.** Show them the fingerprint, let them compare
  it with the one the cluster's operators publish, and let them run `--pin`.
- Exits 1 when the server presents a different key than the one logins trust
  (pinned, or known to ssh). **Stop and tell the user**; such a key can only be
  pinned from a terminal.

## `run` — run a command through the tunnel

```
ctun -t <cluster> run [--tty] [-n|--dry-run] [--project NAME] [--note TEXT] -- <command> [args...]
```

- Everything after `--` is forwarded to the cluster **exactly as written**.
  ctun's own options (`--tty`, `-n`, `--project`, `--note`) go before the
  command: after its first word, every option belongs to the command.
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

### Job records

For every `Submitted batch job <N>` line in the output, `run` stores a
**job record** on the cluster, in `~/.ctun/jobs/<Slurm ClusterName>/<N>.json`,
unless Slurm took job `<N>` before the command started (`run -- cat submit.log`
prints old IDs; they get no record).
Every machine that submits to that cluster sees the same records. It holds:

- `--project NAME` and `--note TEXT`, if given (both optional);
- facts ctun collects itself, never from you: the job's name and working
  directory (from Slurm), and the git state there at submit time (`commit`,
  `branch`, `pushed`, `uncommitted` files: at most 100, with
  `uncommitted_omitted` counting the rest), plus the command, the submitting
  host and the time;
- `note_confirmed`, true once the human confirmed the note, and `changes`, the
  log of later edits (see [`jobs edit`](#jobs-edit--change-jobs-project-or-note)).

The project and note can be changed later with `ctun jobs edit`; the facts never.

A fact ctun cannot read is stored as `null` with a warning; it never blocks the
submission. The command's output and exit code are unchanged. Records are off
with `--tty` (the output is not read) and for a cluster with
`job_records: false` in the config.

**Notes are drafts for the human.** Put only facts from this session into
`--note` (what changed and why), never guesses about results. Refer to a commit
as `commit:<hash>` and to a job as `job:<id>`. ctun checks each job reference
before it runs the command, and refuses the run with exit code 13
(`ctun-error: note_reference_invalid`) if one cannot be found. Commit references
are checked after the submission, in the new job's repo and in those of the
project's earlier records; one that cannot be found is stored as written, with a
warning on stderr: fix it with `jobs edit --note`. When unsure, leave the note
out.

```
ctun -t haicore run --project htfm --note "lr 1e-3 after commit:541b4cb, rerun of job:1695485" -- bash -c 'cd $REPO && ./submit.sh'
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

A command whose output had `Submitted batch job <N>` lines also shows the job
IDs, e.g. `sbatch train.sh  → job 4818213`; in the JSON, that command carries
`"job_ids": ["4818213"]`. Each ID is logged as soon as the line is printed, so
`sbatch --wait` and an interrupted run keep theirs. ctun only reads the output:
any such line counts (`run -- cat submit.log` logs the IDs in that log),
`sbatch --parsable` prints none to find, and the ID of a job sent elsewhere with
`sbatch -M <cluster>` belongs to that cluster. A `run --tty` logs no job IDs
(ctun does not see its output).

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
ctun config --migrate  # convert old-style entries; shows the diff, asks first (-y/--yes: don't ask)
```

Old configs keep working unchanged; `--migrate` is an optional, user-run
upgrade. It turns each single-string `description` into a list (one note per
blank-line-separated paragraph), keeps the previous file as `config.yaml.bak`,
and does nothing if there is nothing to convert. Without a terminal it only
prints the diff.

A cluster entry looks like:

```yaml
clusters:
  haicore:
    host: haicore.scc.kit.edu
    user: ab1234
    requires_otp: true
    requires_password: true     # optional (default true); false = OTP/key only, no password
    ssh_config: /path/to/config # optional: alternative ssh config file (ssh -F)
    description:                # notes for the agent, shown by `info` (one string also works)
      - HAICORE @ KIT. GPU jobs via Slurm.
      - Always pass --time; there is no default walltime.
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
unless asked. The one exception is `ctun note`, and only with the user's
approval of the exact text.

## JSON output for scripting

`status`, `info`, `logs`, `jobs` and `hostkey` accept `-j/--json` for stable machine-readable
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
| `hostkey`: `DIFFERENT from the pinned key`, or login: `Host key verification failed` | The server presents another key than the pinned one: re-keyed, or someone in between. | Stop. Tell the user; only they re-pin, after checking the published fingerprint. |
| `WSL is not installed` / `WSL distro '…' lacks …` | Windows only: WSL can't carry the tunnel yet. | Show the user the message; they fix WSL. Don't work around it. |
| `note`: `… single-string description, which can't take notes` | The config predates note lists. | Tell the user; `ctun config --migrate` converts it, and that's their call. |

## Windows (experimental)

On Windows, `ctun` runs `ssh` and `rsync` inside WSL 2; every command works the
same. Differences that matter to you:

- **Git Bash rewrites `/paths`.** Arguments starting with `/` become Windows
  paths before `ctun` sees them (`/scratch/x` → `C:/Program Files/Git/scratch/x`),
  which breaks remote paths in `run` and `upload`/`download`. Prefix such calls
  with `MSYS_NO_PATHCONV=1`, unless the user has set it globally.
- **Line endings.** Scripts written or checked out on Windows may have CRLF
  endings; bash on the cluster fails on them and `sbatch` rejects them. Make
  sure `.sh`/batch scripts are LF before uploading.
- **Local paths** for `upload`/`download` may be Windows paths (`C:\data\`,
  `.\out`) or WSL paths (`/home/<user>/data`); trailing slashes keep their rsync
  meaning.
- `identity_file`, `socket_dir` and `ssh_config` in the config are paths
  inside WSL; `wsl_distro` (under `defaults`) picks the distro.
