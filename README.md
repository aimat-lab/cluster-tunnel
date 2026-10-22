![cluster-tunnel banner](banner.jpg)

# cluster-tunnel (`ctun`)

`ctun` keeps an authenticated SSH connection to an OTP-protected HPC cluster **alive
in the background**, so you (and your coding agents) can run commands on the cluster
**without re-entering a one-time password every time** — and it enforces a
**per-session compute budget** so automated job submission can't overspend.

You authenticate once. After that, every `ctun -t <cluster> run -- <command>` reuses
the live connection with zero re-authentication, and any job submission is checked
against a budget before it reaches the scheduler.

Runs on Linux and macOS, and on Windows through WSL 2 (experimental, see
[Windows](#-windows-experimental)).

```console
$ ctun -t horeka login -i              # interactive login once: password + OTP popup
$ ctun -t horeka run -- squeue --me     # reuse the live tunnel, no re-auth
$ ctun -t horeka run -- sbatch train.sh # submitted only if within budget
$ ctun -t horeka status                 # tunnel state + budget used
$ ctun -t horeka logout                 # close the tunnel
```

---

## 📚 Contents

- [How it works](#️-how-it-works)
- [Requirements](#-requirements)
- [Install](#-install)
- [Windows (experimental)](#-windows-experimental)
- [Quickstart](#-quickstart)
- [Commands](#-commands)
- [Configuration](#-configuration)
- [The compute-budget guard](#️-the-compute-budget-guard)
- [Using ctun with coding agents](#-using-ctun-with-coding-agents)
- [Python API](#-python-api)
- [Files and locations](#-files-and-locations)
- [Contributing](#-contributing)
- [License](#-license)

---

## ⚙️ How it works

`ctun` is built on OpenSSH **connection multiplexing** (`ControlMaster` /
`ControlPersist`). Authentication is a property of the *connection*, not of each
command:

- **`login`** opens a single **master connection** and leaves a control socket alive
  in the background. This is the only step that needs your password + OTP.
- Every later **`run`** opens a lightweight channel *inside* that already-authenticated
  connection — no password, no OTP, near-instant.

`ctun` itself is **stateless** — it runs once per command and exits. The persistent
state lives in the background ssh master, a small config file, and a per-cluster
session record. The connection lives as long as the network and the cluster's session
policy allow; when it eventually drops, `run` fails with a clear message and you simply
`login` again.

---

## 📋 Requirements

- **Linux or macOS** with an **OpenSSH client** (`ssh`) — version 7.x+.
- **Windows 10/11 (experimental):** WSL 2 with a Linux distro that has `ssh`,
  `rsync` and `python3` ≥ 3.10 — see [Windows](#-windows-experimental).
- **Python ≥ 3.10**.
- For the optional pop-up login dialog (`login --interactive`): a graphical display and
  a working **Tk** (provided by your system Python's `tkinter`).

---

## 📦 Install

Using [uv](https://docs.astral.sh/uv/) (recommended — installs `ctun` onto your PATH):

```console
$ git clone https://github.com/aimat-lab/cluster-tunnel
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

## 🪟 Windows (experimental)

No Windows build of OpenSSH can share one login across commands (ControlMaster),
so on Windows `ctun` runs `ssh` and `rsync` inside **WSL 2**. Only the
connection lives there: install `ctun` on Windows as above, and your editor,
your agent and every `ctun` command stay on Windows.

**Setup**

1. Install WSL 2 with a distro (`wsl --install` in an administrator PowerShell)
   and make sure it has the tools: `sudo apt install openssh-client rsync python3`
   (Ubuntu ships them).
2. Keep your SSH setup inside WSL. ssh there reads WSL's `~/.ssh/config` and
   keys, not the Windows ones. Copy keys into WSL's `~/.ssh` and `chmod 600`
   them: keys on the Windows drive (`/mnt/c/...`) fail OpenSSH's permission check.
3. `ctun -t <cluster> login` checks WSL first and names the distro it uses.

**Config on Windows**

- `identity_file`, `socket_dir` and `ssh_config` are **paths inside WSL**
  (`~/.ssh/id_ed25519`, not `C:\Users\...`); `ctun config --validate` flags
  Windows-style ones.
- `ssh_config` (under `defaults`, or per cluster) points ssh at another config
  file, e.g. your Windows one: `/mnt/c/Users/<you>/.ssh/config`. Use an
  absolute path; ssh does not expand `~` there.
- `wsl_distro` (under `defaults`) picks a distro other than WSL's default.

`upload` and `download` accept Windows paths (`C:\data\`, `.\results`) and WSL
paths (`/home/you/data`) on the local side; a trailing slash keeps its rsync
meaning.

**Caveats**

- Git Bash (which Claude Code uses on Windows) rewrites arguments that start
  with `/` into Windows paths before `ctun` sees them: `run -- ls /scratch`
  would send `C:/Program Files/Git/scratch`. Set `MSYS_NO_PATHCONV=1`, e.g. as
  a Windows user environment variable, to turn that off.
- Files checked out by Git on Windows often get CRLF line endings, which bash
  on the cluster chokes on (`sbatch` refuses them outright). Keep `.sh` and
  batch scripts LF, e.g. with `*.sh text eol=lf` in `.gitattributes`.
- There is no logout hook on Windows: a reboot ends the tunnel, and the next
  `login` starts a fresh session.
- Tested against local SSH servers, not yet against a real OTP cluster from
  Windows.

---

## 🚀 Quickstart

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
    requires_otp: true            # set false for clusters that don't use an OTP
    description:                  # notes for the agent, shown by `ctun info`
      - HoreKa @ KIT. GPU jobs go to partition 'accelerated'.
```

> The starter config (`ctun config --init`) already ships several commented
> example clusters. It's usually easiest to just **uncomment one and fill in your
> own username** rather than write a block from scratch.

**3. Log in once** — `-i/--interactive` pops up a small window for your password + OTP
(and works even when `ctun` has no terminal of its own, e.g. when an agent runs it):

```console
$ ctun -t horeka login -i
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

## 🧰 Commands

The cluster is selected with the global **`-t` / `--target`** option, placed **before**
the subcommand: `ctun -t <cluster> <command> …`.

Global options:

| Option | Meaning |
|---|---|
| `-t, --target <name>` | Cluster to act on (a key under `clusters:` in the config). |
| `-c, --config <path>` | Use an alternate config file (overrides `$CTUN_CONFIG`). |
| `-V, --verbose` | Enable debug logging. |
| `--version` | Print the version. |

### `status` — tunnel and session state

```console
$ ctun -t horeka status              # one cluster (detailed)
$ ctun status                        # all configured clusters
$ ctun -t horeka status -j|--json    # machine-readable
```

Shows whether the tunnel is live, when the session started, and the budget limit.
Use it first whenever you lack context.

### `config` — manage the config file

```console
$ ctun config                  # open in $EDITOR
$ ctun config -i|--init        # create a starter config if none exists
$ ctun config -p|--path        # print the config file path
$ ctun config -s|--show        # print the resolved config
$ ctun config --validate       # check for errors + warn on missing budget
                               # scripts / invalid guard_commands regexes
$ ctun config --migrate        # convert old-style entries to the current format:
                               # shows the diff, asks first (-y/--yes: don't ask)
```

Configs written for older `ctun` versions keep working as they are. `--migrate` is
an optional upgrade you run yourself: it turns each single-string `description` into
a list of notes (one per blank-line-separated paragraph), keeps the previous file as
`config.yaml.bak`, and does nothing when there is nothing to convert. Older `ctun`
versions (0.5.0 and earlier) can't read a migrated config.

### `login` — authenticate and open the tunnel

```console
$ ctun -t horeka login [-i|--interactive] [-l|--limit N] [--timeout S]
```

- Opens the persistent master connection.
- `-i, --interactive` **(recommended)** shows a small pop-up window (password + OTP +
  limit) instead of prompting in the terminal, then feeds the secrets to ssh for you.
  This is the easiest way to log in, and the only one that works when `ctun` is invoked by
  a tool that has no terminal of its own (e.g. a coding agent) — a person at the machine
  fills in the window. The password field may be left blank for clusters that need no
  password. `--timeout S` bounds how long it waits (default 120s).
- Plain `ctun -t <cluster> login` (no `-i`) prompts for the password + OTP in the terminal.
- `-l, --limit N` sets this session's compute budget (overrides the cluster's configured
  `session_limit`).

A **session** lasts for the lifetime of one authentication. Logging in again (after the
tunnel drops) starts a fresh session and resets the budget window.

### `hostkey` — check and pin the server's host key

```console
$ ctun -t horeka hostkey              # the key the server presents, and what a login makes of it
$ ctun -t horeka hostkey -j|--json    # machine-readable
$ ctun -t horeka hostkey --pin        # pin it: shows the change, asks first (-y/--yes: don't ask)
```

Until a key is pinned, logins accept an unknown server key on first contact (ssh's
`StrictHostKeyChecking=accept-new`), which trusts whoever answers the first time.
`hostkey` meets the server over fresh connections that stop before logging in, so the
cluster asks no password or OTP (a ProxyJump host in your ssh config logs in as usual).
ssh itself then says what a login would make of the key: one it trusts, a new one, or
one that differs from the key it trusts, judged against the same known_hosts files a
login uses.

Compare the fingerprint it shows with the one your cluster's operators publish; then
`--pin` stores the key in a `known_hosts` file next to the config and sets
`strict_host_key_checking: 'yes'` and `known_hosts_file` in the cluster's config entry
(keeping the previous config as `config.yaml.bak`). From then on that file is the only
list of trusted keys for the cluster: ssh's own known_hosts files and DNS records no
longer count, and every ssh `ctun` starts for the cluster checks it. Versions before
0.7.0 can't read a config with these keys.

`hostkey` exits 1 when the server presents a different key than the one logins trust
(pinned, or known to ssh from earlier): a re-keyed server, or someone in between. Find out
which before logging in; such a key can only be pinned from a terminal (`--yes` does not
apply to it).

### `run` — run a command through the tunnel

```console
$ ctun -t horeka run [--tty] [-n|--dry-run] -- <command> [args…]
```

- Everything after `--` is forwarded to the cluster exactly as written, so flags like
  `--me` or `--time=01:00:00` pass straight through. `ctun`'s own options go before the
  command: after its first word, every option belongs to the command, `--` or not.
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
| `13` | `note_reference_invalid` | `run --note` names a `job:<id>` the cluster does not know; nothing ran. (`jobs edit --note` also for a `commit:<hash>`; nothing was saved.) |
| *N* | — | The remote command's **own** exit code (0–255), propagated unchanged. |
| `2` | — | Usage error (e.g. no command after `--`). |

```console
$ ctun -t horeka run -- sbatch big.sh ; echo "exit=$?"
ctun-error: budget_exhausted
Error: Submission blocked on 'horeka': compute budget exhausted.
  ...
exit=11
```

### `upload` / `download` — transfer files over the tunnel

```console
$ ctun -t horeka upload [-n|--dry-run] <local-src> <remote-dest> [-- <rsync args>]
$ ctun -t horeka download [-n|--dry-run] <remote-src> <local-dest> [-- <rsync args>]
```

Move files in or out using **rsync over the live tunnel** — it rides the existing
authenticated connection, so there's **no re-authentication** (no password, no OTP).

- **`upload`** copies a local path → the cluster; **`download`** copies a cluster path →
  local. The direction decides which side is remote, so remote paths are written **bare**
  (no `host:` prefix); a relative remote path resolves to your remote `$HOME`.
- Copies **recursively** (`rsync -r`). Trailing slashes follow rsync's semantics: `data/`
  copies the *contents* of `data`, `data` copies the directory itself.
- Transfers are **not** subject to the budget guard (moving data isn't compute), and they
  **fail closed**: with no live tunnel the command exits `10` (`login_required`) rather than
  prompting.
- rsync's own exit code is propagated. Extra rsync flags can follow `--`, e.g.
  `-- --exclude='*.tmp' -z` or `-- --info=progress2` for a progress display.

```console
$ ctun -t horeka upload ./dataset $WORK/dataset       # push a folder
$ ctun -t horeka download logs/run42.out ./run42.out  # pull a result file
```

### `info` — briefing for a cluster

```console
$ ctun -t horeka info [-j|--json]
```

Prints the cluster's description, its (advisory) restrictions, and the current budget
usage vs. limit. Works even without a live tunnel. A description that is a list prints
as bullet points; in `--json` output it stays a list (a string stays a string).

### `note` — add a note to a cluster's description

```console
$ ctun -t horeka note "Pass -A hk-project-x to sbatch."
```

Appends the text to the cluster's `description` list in your config, with a
`# added via ctun note, <date>` comment, so `ctun info` shows it from then on. Only
those lines of the file change; your comments and layout stay as they are. Meant for
an agent to record what it learned about a cluster — the skill makes it ask you for
the exact text first. A description that is still a single string has to be
converted with `ctun config --migrate` first.

### `logout` — close the tunnel

```console
$ ctun -t horeka logout              # one cluster
$ ctun logout                        # all configured clusters
```

Tears down the master connection and clears the session record. Without a
target, it logs out every configured cluster.

To run `ctun logout` **automatically on every logout, reboot, or shutdown**,
install the systemd user hook in [`systemd/`](./systemd/) (`cd systemd &&
./install.sh`; Linux only). See [systemd/README.md](./systemd/README.md) for details.

---

## 🔧 Configuration

Config lives at `~/.config/cluster-tunnel/config.yaml` (override with `$CTUN_CONFIG` or
`-c/--config`). A fully annotated example:

```yaml
# Defaults inherited by every cluster (each is overridable per cluster).
defaults:
  control_persist: "7d"           # how long an unused tunnel stays up; "yes" = until reboot/logout
  server_alive_interval: 60       # keepalive seconds; fights idle disconnects
  server_alive_count_max: 10      # missed keepalives before giving up: rides out ~10 min outages
  # ssh_config: ~/.ssh/config_hpc # optional: alternative ssh config file (ssh -F); also per cluster
  # wsl_distro: Ubuntu            # Windows only: WSL distro that runs ssh (default distro if unset)
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
    strict_host_key_checking: 'yes'   # set by `ctun hostkey --pin`; default: accept-new
    known_hosts_file: ~/.config/cluster-tunnel/known_hosts   # where the pinned key is kept
    control_persist: "24h"        # optional per-cluster override

    description:                  # notes for the agent, shown by `ctun info` (or one string)
      - HoreKa @ KIT. GPU jobs go to partition 'accelerated'.
      - Node-local scratch is $TMPDIR; stage large inputs there.

    restrictions:                 # advisory only — shown by `ctun info`, never enforced
      max_job_runtime: "48:00:00"
      max_gpus_per_job: 4
      allowed_partitions: [accelerated, cpuonly]

    budget:                       # omit this whole block to run the cluster unguarded
      script: budget/horeka.sh    # script that reports usage since the session started
      session_limit: 500          # default budget; overridden by `login --limit`
      unit: jobh                  # label for display (bundled scripts report job-hours)
      guard_commands: [sbatch, srun, salloc]   # commands subject to the budget check
      guard_scope: every-command  # also inside `bash -c`, after env/timeout/xargs (default: first-command)
      fail_mode: closed           # if the budget script errors: "closed" blocks, "open" allows
```

**Connecting:** if `ssh_alias` is set, `ctun` uses that `~/.ssh/config` Host entry for the
hostname, user, and key; otherwise it uses `host` + `user` (+ optional `identity_file`).

**Host keys:** `strict_host_key_checking` is `accept-new` unless you pin the cluster's key
with `ctun hostkey --pin`, which sets it to `'yes'` together with `known_hosts_file`. With
`'yes'`, a login accepts only a key it already knows. A `known_hosts_file` makes that file
the only one consulted (not ssh's own files, the global one or DNS); write it as a plain
path (`~/` is fine). These options override the same ones in your ssh config.

**Restrictions** are informational. They are shown by `ctun info` as guidance but are
never enforced by `ctun` — the only hard enforcement is the budget guard below.

---

## 🛡️ The compute-budget guard

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

By default only the **first word** counts, so `bash -c 'cd run && sbatch job.sh'` or
`timeout 60 srun …` pass unchecked. Set `guard_scope: every-command` in a cluster's
`budget` to have the guard look wherever a command can start:

- in a script given to `bash -c`, `sh -c`, `eval`, `su -c`, `env -S` or `watch`, or the
  command line of `ssh <host>`: after `;`, `&&`, `||`, `|`, `&`, braces and newlines, and
  inside `$(…)` and backticks, even quoted ones. Comments, here-documents, redirection
  targets and other quoted text are not commands;
- the command a wrapper such as `env`, `nohup`, `timeout`, `nice`, `xargs`, `flock` or
  `sudo` runs, after the wrapper's own options and arguments (`timeout 30 grep sbatch log`
  runs grep, not sbatch);
- after `find -exec`.

A command nested too deeply to follow counts as a submission. The guard can't see into a
script file (`bash submit_all.sh`, `./submit.sh`), a program's own code, or commands fed
on stdin.

The default metric is **job-hours**: the Slurm job *wall-clock* time you have run since the
session started, counting only the portion of each job inside the session window (concurrent
jobs add up). The bundled budget scripts (`ctun config --init` copies them next to your
config) compute this from `sacct` and work on any cluster with Slurm accounting enabled.

You normally don't write a budget script yourself — `ctun config --init` copies the bundled
ones next to your config. For a different metric (GPU-hours, core-hours, …) adapt a bundled
script: it runs on the login node, receives `$1` session-start epoch, `$2` cluster, `$3`
username, and must print a single number. If it exits non-zero, `fail_mode` decides
(`closed` blocks the submission, `open` allows it). See [DESIGN.md](./DESIGN.md) for details.

Example of the guard in action:

```console
$ ctun -t horeka run -- sbatch big.sh
BLOCKED on horeka: session budget exhausted: 503.2 >= 500.0 jobh. Command not submitted.
```

---

## 🤖 Using ctun with coding agents

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
- **Notes grow with use.** When the agent learns something lasting about a cluster, it
  proposes a note; once you approve the exact text, `ctun note` adds it to that
  cluster's description, and every later session sees it in `info`.

All agents must run `ctun` on the **same machine** where the tunnel was opened, since the
control socket is local to that machine.

---

## 🐍 Python API

Programs can call what `ctun` does directly. `cluster_tunnel.api` has a function for each
command, and the commands themselves are built on it, so the tunnel check, the budget
guard, the session and the command log behave exactly as on the command line.

```python
from cluster_tunnel import api

if api.is_live("horeka"):
    result = api.run("horeka", ["squeue", "--me"], capture=True)
    print(result.exit_code, result.stdout)

try:
    api.run("horeka", ["sbatch", "train.sh"], capture=True)
except api.LoginRequired:
    ...  # a person runs `ctun -t horeka login`
except api.BudgetBlocked as exc:  # BudgetExhausted or BudgetGuardError
    print(exc.decision.reason)
```

| Function | Does what |
|---|---|
| `login`, `logout`, `status`, `is_live` | `ctun login`, `logout`, `status`; the tunnel state. `login(credentials=api.Credentials(password, otp))` takes a password and OTP from the program's own prompt instead of the terminal or the dialog |
| `run`, `preflight` | `ctun run` (streamed, or `capture=True` with an optional `timeout`) and `run -n`; `project=`/`note=` go into the job records, and the `Result` lists the submitted `job_ids` and the `records` written |
| `upload`, `download` | `ctun upload`, `download` (optionally `capture=True`) |
| `jobs` | `ctun jobs`; `with_records=True` also reads each job's record |
| `edit_job` | `ctun jobs edit` for one job: a new project or note in its record |
| `projects` | `ctun projects list`: every project in the job records, with its number of jobs |
| `rename_project` | `ctun projects rename`: a project's new name in every job record, nested projects included |
| `host_key`, `pin_host_key` | `ctun hostkey`, `hostkey --pin`; pins only the fingerprint passed in, and replaces a trusted key only with `replace=True` |
| `budget` | The guard's verdict for any submission right now, probed over the tunnel |
| `feed_script` | Runs a local bash script on the login node; not guarded, for read-only probes |
| `connection`, `clusters`, `load_config` | The resolved ssh target and control socket; the config |

A program with its own login prompt (a window's masked field) hands the secrets
straight to ssh; `Credentials` never prints them, and a cluster that requires a password
refuses the login without one before ssh starts. The OTP is advisory, as in the dialog.
When the login fails, the `LoginError`'s `diagnostics` say why if ssh did (a refused
host key, say, which no retyped password fixes):

```python
try:
    api.login("horeka", credentials=api.Credentials(password, otp))
except api.LoginError as exc:
    show(str(exc), exc.diagnostics)
```

Failures raise `api.ClusterTunnelError` subclasses. The ones `ctun` reports with a stable
exit code carry it as `exit_code` and `marker`: `LoginRequired` (10), `BudgetExhausted`
(11), `BudgetGuardError` (12) and `NoteReferenceInvalid` (13). Each function reads the same config as `ctun`
(`config_path=`, else `$CTUN_CONFIG`, else the default location) and, like `ctun`, must
run on the machine that holds the tunnel. `cluster_tunnel.api` and `cluster_tunnel.errors`
are the public interface and follow semantic versioning.

---

## 📁 Files and locations

| Path | Purpose |
|---|---|
| `~/.config/cluster-tunnel/config.yaml` | Your configuration. |
| `~/.config/cluster-tunnel/config.yaml.bak` | The config as it was before the last `ctun config --migrate` or `ctun hostkey --pin`. |
| `~/.config/cluster-tunnel/budget/<cluster>.sh` | Per-cluster budget scripts. |
| `~/.config/cluster-tunnel/known_hosts` | Host keys pinned with `ctun hostkey --pin`, next to the config in use (on Windows, inside WSL). |
| `~/.cache/cluster-tunnel/sockets/<cluster>` | SSH control sockets (the live tunnels). |
| `~/.cache/cluster-tunnel/sessions/<cluster>.json` | Internal session state (start time, limit). |

On Windows, the config lives in `%LOCALAPPDATA%\cluster-tunnel`, the cache in its
`Cache` subfolder, and the control sockets inside WSL, at `~/.cache/cluster-tunnel/sockets/` there.
Installs from 0.5.0 keep using `%LOCALAPPDATA%\jonas\cluster-tunnel` until the new
folder exists; move the folder to switch over.

---

## 🤝 Contributing

If there is a feature you would like to see, or you run into any issue with the
program, please [open an issue](https://github.com/aimat-lab/cluster-tunnel/issues/new)
— feedback of any kind is very welcome.

---

## 📄 License

cluster-tunnel is released under the [MIT License](./LICENSE).

---

*Design notes: see [PLAN.md](./PLAN.md) for the motivation and [DESIGN.md](./DESIGN.md)
for the detailed technical specification.*
