# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `skill/writing-notes.md`: how an agent writes a job's `--note`, with
  examples. It reads the user's own `writing-notes.local.md` beside it first,
  which this repo never ships, so updating the skill keeps it.

## [0.9.0] - 2026-10-22

### Added

- **Each job's start time.** `ctun jobs --json` and `api.jobs()` report a job's
  `start` as Slurm prints it: when it started, or for a pending job Slurm's
  estimate (`N/A` while it has none).
- **Each cluster's Slurm name.** `ctun jobs --json` and `api.jobs()` name each
  cluster's Slurm `ClusterName` in `slurm_cluster`, the value jobs see as
  `SLURM_CLUSTER_NAME` (`null` when unknown), so a front end can match a job to
  tags like `slurm:<ClusterName>:<job id>`.
- **The job IDs a command submitted.** The command log keeps the IDs of the jobs
  each `ctun run` submitted (every `Submitted batch job <N>` line of its
  output). `ctun logs` shows them after the command (`→ job 4818213`), `ctun
  logs --json` gives them as `job_ids`, and `api.run()` returns them in
  `Result.job_ids`. The output still streams as it arrives, and each ID is
  logged as soon as it is printed, so `sbatch --wait` and an interrupted run
  keep theirs. Any such line counts, so `run -- cat submit.log` logs the old IDs
  in it; `sbatch --parsable` prints none, and the ID of a `sbatch -M <other>`
  job belongs to that cluster, not to `slurm_cluster`. A `--tty` run logs none:
  ctun does not see its output. Older logs still read.
- **Job records.** For each `Submitted batch job <N>` in its output, `ctun run`
  stores a record on the cluster in `~/.ctun/jobs/<Slurm ClusterName>/<N>.json`,
  so every machine sees it (not for a job Slurm took before the command started,
  as in `cat submit.log`). It holds the new optional `--project` and `--note`
  (`api.run(project=, note=)`), and facts ctun collects itself: job name and
  working directory from Slurm, and the git commit, branch, pushed state and
  uncommitted files there at submit time (at most 100 files, and
  `uncommitted_omitted` counting the rest). A failing fact only warns
  (`Result.warnings`). `ctun jobs --json` (`api.jobs(with_records=True)`)
  attaches each job's `record` (the tasks of an array share the array's) and
  lists `records_without_job` per cluster. Turn it off per cluster with
  `job_records: false`.
- **Checked references in notes.** `ctun run --note` checks its `job:<id>`
  references on the cluster before running, and refuses with the new exit code
  13 (`note_reference_invalid`, `api.NoteReferenceInvalid`) if one is unknown.
  Its `commit:<hash>` references are checked once the jobs are submitted, in
  their own repos and those of the project's earlier records; an unknown one
  only warns.
- **Editing a job's project and note.** `ctun -t <cluster> jobs edit <id>...`
  with `--project`, `--note` or `--confirm-note` (`api.edit_job()`, and
  `api.edit_jobs()` for several) gives one or more jobs' records the same change
  on the cluster, so every machine and agent sees it, and prints the resulting
  records as a JSON list. An empty value clears it (Unassigned, no note). A
  changed note is unconfirmed until `--confirm-note`. Each edit is logged in the
  record's `changes` as old and new value; the facts from submit time are never
  changed. For a job that has no record (e.g. one submitted with plain
  `sbatch`), `jobs edit` creates one without facts, so older jobs can join
  projects too, and a task of an array gets a copy of the array's record. With
  several IDs every job is checked first, so an unknown job or reference changes
  none of them. Records gain `note_confirmed` and `changes`. `ctun jobs list`
  names the overview next to `jobs edit`; plain `ctun jobs` works as before.
- **Projects of all time.** `ctun projects list` (`api.projects()`) lists every
  project in the job records of each live cluster, however old, with its number
  of jobs; `--json` for front ends such as the VS Code extension's Projects
  view. A cluster configured without records is marked `"job_records": false`,
  with `"live": null` (not checked).
- **Renaming a project.** `ctun projects rename OLD NEW`
  (`api.rename_project()`) renames a project in every job record of each live
  cluster, with the projects nested in it (`htfm/mh` → `archive/htfm/mh`). Each
  change is logged in the record like `jobs edit`, and the records go back a few
  dozen per SSH call (`records.sh update` takes several records). A record
  another machine edits meanwhile is renamed again from its new state, not
  overwritten; `jobs edit` does the same. If storing fails partway, `renamed`
  still lists what was stored. It exits 1 when the rename failed, and 10 with
  `-t` when the tunnel is down. Running it again is safe, also when NEW lies
  inside OLD (`htfm` → `htfm/old`). Project names lose a `/` or blanks at either
  end wherever they are stored (`archive/` is `archive`).
- **Credentials on stdin.** `ctun login --credentials-stdin` reads the password,
  OTP and session limit as JSON on stdin and logs in with them, as
  `api.login(credentials=...)` does: for front ends such as the VS Code
  extension, whose own input boxes ask the human.
- **`login -i` through a front end.** `ctun login -i` hands the login to the
  front end that started it, such as a VS Code window with the ctun extension,
  when that names its local pipe in `CTUN_LOGIN_BRIDGE`; so does
  `api.login(interactive=True)`, whose `LoginResult.bridged` says so. The front
  end asks for the secrets and logs in itself with `ctun --config <config> login
  --credentials-stdin`, using the config file the request names; only the
  outcome comes back, and ctun checks that the tunnel is really up. `--timeout`
  bounds the wait. Without the variable, or if the front end declines or is
  gone, the popup opens as before.

### Changed

- **`ctun run` options go before the command.** ctun's own options end at the
  command's first word, so `ctun run python train.py --note x` passes `--note x`
  to `train.py` instead of taking it as the job's note. So does a `--tty` or
  `-n` after the command: write `ctun run --tty -- htop`, not `ctun run htop
  --tty`.

## [0.8.0] - 2026-10-20

### Added

- **Login with credentials from the caller.** `api.login(cluster,
  credentials=api.Credentials(password, otp))` opens the tunnel with a password and OTP
  that a program asked for itself, such as a window's masked field: they go to ssh as
  the login dialog's do, with no dialog and no terminal. A cluster that requires a
  password refuses the login without one before ssh starts; the OTP is advisory, as in
  the dialog. A `limit` in the credentials stands in for the `limit` argument.
  `credentials` with `interactive=True` is a `ValueError`.

### Changed

- **A failed password login says why in the error.** When a login through the dialog
  or with `credentials` fails, what ssh said about it (a refused host key; with
  `verbose`, all its diagnostics) is in `LoginError.diagnostics` instead of being
  written to the process's stderr, so a program with its own window can show it.
  `ctun login -i` prints it as before.

### Security

- **Credentials never print.** `api.Credentials`, which is also the login dialog's
  answer, shows only which secrets are set in its repr, so a log line or a traceback
  that holds one leaks neither the password nor the OTP.
  The reason a failed login gives masks both, in case the server echoed one.

## [0.7.1] - 2026-10-20

### Changed

- **Tunnels last longer by default.** `control_persist` now defaults to `7d` (was
  `12h`): an unused tunnel stays up for a week after its last command, so it
  survives a night or a weekend without a new login. `server_alive_count_max` now
  defaults to `10` (was `3`): with the 60 s keepalive, a tunnel rides out about 10
  minutes without a reply from the server (a VPN reconnect, a brief network outage)
  instead of 3. A dead connection takes correspondingly longer to be noticed. Configs
  that set either key keep their value; a config written by `ctun config --init`
  before 0.7.1 sets `control_persist: "12h"` explicitly. The new values apply from the
  next `ctun login`.

## [0.7.0] - 2026-10-20

### Added

- **License.** cluster-tunnel is now released under the MIT License (`LICENSE`), so
  it can be used as a dependency and redistributed.
- **Python API.** `cluster_tunnel.api` offers each command as a function (`login`,
  `logout`, `status`, `run`, `preflight`, `upload`, `download`, `jobs`), plus `budget`
  (the guard's verdict for any submission, probed now), `feed_script`, `connection`
  and `is_live`. `run` and the transfers can capture their output instead of
  streaming it, `run` with a timeout. Failures raise `cluster_tunnel.errors`
  exceptions; those with a stable exit code carry it (`LoginRequired` 10,
  `BudgetExhausted` 11, `BudgetGuardError` 12). The API and `cluster_tunnel.errors`
  are the public interface and follow semantic versioning.
- **Host-key pinning.** `ctun -t <cluster> hostkey` shows the host key the server
  presents and what a login would make of it, as ssh itself judges it against the
  known_hosts files a login uses: trusted, new, or different from the trusted key (exit
  1). It connects without logging in, so the cluster asks no password or OTP. `--pin`
  stores the key in a `known_hosts` file next to the config and sets the new cluster
  keys `strict_host_key_checking: 'yes'` and `known_hosts_file`, after showing the
  change and asking (`-y/--yes` without a terminal; never for a changed key). That file
  is then the only list of trusted keys for the cluster (not ssh's own files, the
  global file or DNS), on every ssh `ctun` starts for it. ctun 0.6.0 and older can't
  read a config with these keys. In the API: `host_key`, and `pin_host_key`, which
  pins only the given fingerprint and replaces a trusted key only with `replace=True`.
- **Budget guard scope.** A cluster's `budget` can set `guard_scope: every-command` so
  the guard finds `guard_commands` wherever a command can start, not only in the first
  word: in scripts given to `bash -c`, `sh -c`, `eval`, `su -c`, `env -S`, `watch` or
  `ssh <host>` (split as a shell splits them: comments, here-documents and redirection
  targets aside, `$(…)` and backticks included), in the command a wrapper such as
  `env`, `nohup`, `timeout`, `xargs` or `sudo` runs, and after `find -exec`. A command
  nested too deeply to follow counts as a submission. The default, `first-command`,
  keeps the previous behaviour. `ctun info` shows the scope.

### Changed

- `login`, `logout`, `status`, `jobs`, `run`, `upload` and `download` now run through
  the Python API. Their output, exit codes and `ctun-error:` markers are unchanged;
  new contract tests pin them, and every scenario is checked through both the CLI
  and the API.
- The tunnel's host-key checking now follows the cluster's
  `strict_host_key_checking` (default `accept-new`, as before) instead of a fixed
  `accept-new`.
- When `login --interactive` fails because ssh refused the server's host key, ssh's
  lines saying so are shown even without `-v`.
- Probes over the tunnel (budget usage, job list) give up after 120 seconds instead of
  waiting forever; a budget probe that times out is handled per `fail_mode`.

## [0.6.0] - 2026-10-14

### Added

- **Cluster notes.** A cluster's `description` can now be a list of notes as well as
  one string. `ctun info` prints a list as bullet points, and `info --json` keeps
  whichever form the config uses, so existing configs and JSON readers are unaffected.
- `ctun -t <cluster> note "<text>"` appends a note to that list in `config.yaml`, with
  a `# added via ctun note, <date>` comment. Only those lines change: comments and
  layout elsewhere stay as they are, and the edited file must parse to exactly the
  intended change before it is written. The skill has agents propose notes and run
  `note` only after the user approves the exact text.
- `ctun config --migrate` converts old-style entries to the current format; for now,
  single-string descriptions into lists (one note per blank-line-separated
  paragraph). It shows a diff, asks before writing (`-y/--yes` skips the question;
  without a terminal it only previews), and keeps the previous file as
  `config.yaml.bak`. Old configs keep working without it; once migrated, ctun 0.5.0
  and older can't read the file. `ctun config --validate` mentions when a migration
  is available.
- A description list item that YAML doesn't read as text (`- Account: x` is a
  mapping, `- yes` a true/false value) is reported by item number, with a hint to
  quote it.

### Changed

- On Windows, new installs keep the config and cache in `%LOCALAPPDATA%\cluster-tunnel`
  instead of `%LOCALAPPDATA%\jonas\cluster-tunnel`: appdirs no longer gets an
  author name, which only Windows puts in the path. Existing installs keep
  working from the old folder until the new one exists, and `ctun config` notes
  when the old one is in use. To switch over, move the folder:
  `move %LOCALAPPDATA%\jonas\cluster-tunnel %LOCALAPPDATA%\cluster-tunnel`.
  Linux and macOS are unchanged.

## [0.5.0] - 2026-10-08

### Added

- **Windows support (experimental).** No Windows OpenSSH build can multiplex
  sessions over a control socket, so on Windows `ctun` runs `ssh` and `rsync`
  inside WSL 2 via `wsl.exe`, while `ctun` itself, the editor and the agent stay
  on Windows. `login` first checks that WSL 2 has `ssh`, `rsync` and
  `python3` ≥ 3.10, names the distro it uses, and fails with actionable
  messages otherwise. The interactive login's pty driver runs inside WSL
  (`python3 -I`), with the password/OTP passed on stdin, never in argv or the
  environment; `wsl.exe` is started by its full System32 path.
  `upload`/`download` translate the local path for WSL and keep trailing
  slashes. On Windows, `identity_file`, `socket_dir` and `ssh_config` are paths
  inside WSL; `config --validate` flags Windows-style ones.
- `ssh_config` config key (under `defaults` and per cluster): an alternative ssh
  config file, passed to every ssh call as `-F`.
- `wsl_distro` config key (under `defaults`, Windows only): the WSL distro that
  runs ssh; WSL's default distro if unset.
- `.gitattributes` keeps `*.sh` LF in Windows checkouts, so the bundled probes
  still run under Linux bash.
- CI: GitHub Actions runs the test suite on Ubuntu and Windows. It sets
  `PYTHONWARNDEFAULTENCODING=1`, and `[tool.pytest.ini_options]` turns the
  resulting warnings into errors in ctun's own modules, so text I/O that relies
  on the platform's default encoding fails the build on any OS.

### Changed

- The interactive login's prompt-answering loop moved from `popup.py` into the
  standard-library-only `cluster_tunnel/pty_login.py`, so it can also run
  inside WSL; Linux and macOS run it in-process as before.
- A missing `ssh` (or `wsl.exe`) now reads as "tunnel down" instead of a
  traceback, and `ssh -O check`/`-O exit` give up after 60 seconds.

### Security

- The login dialog and its Tk probe now run Python in isolated mode (`-I`).
  Before, the working directory (typically the agent's project) was on
  `sys.path`, so a planted module such as `json.py` could run inside the
  process that receives the password and OTP.

### Fixed

- `login --interactive` could lose the tunnel right after a successful login
  (4 of 25 attempts in a local test). When `ssh -f` backgrounds itself, its
  exiting parent hangs up the pty, and that SIGHUP could kill the new master
  before it left the session. ssh now starts with SIGHUP ignored; on Windows
  the same applies to plain `login`, since `wsl.exe` also gives ssh a pty.
- `login --interactive` no longer answers a banner or status line that merely
  mentions "password" or "OTP": only the unfinished last line counts as a
  prompt, once the output pauses (a line arriving in pieces is read whole
  first). With `-v`, ssh's debug output now goes to a log file (shown when the
  login fails) instead of the pty, where its lines were taken for prompts, so
  `login -i -v` works.
- A rejected password or OTP (ssh asks again) now ends `login --interactive`
  at once instead of after `--timeout` (120 s by default).
- `ctun --help` (and anything else that prints the logo) no longer crashes with
  `UnicodeEncodeError` when stdout uses a legacy code page, as a piped stdout
  does on Windows (cp1252). The `ctun` console script now points at
  `cluster_tunnel.cli:main`, which switches stdout/stderr to UTF-8 before the
  CLI starts.
- Text I/O no longer depends on the platform's default encoding: the config
  file, the session and command-log files, and captured ssh output are read and
  written as UTF-8 explicitly.
- `cluster_tunnel.popup` imports on Windows: `pty` (which needs `termios`) is
  imported only inside the pty-driven login loop. On Windows the login dialog
  no longer requires `DISPLAY`/`WAYLAND_DISPLAY` and always runs under the
  current interpreter.
- Tests run on Windows: platform-neutral path assertions, shell scripts run
  through an explicitly resolved bash (Git for Windows' bash, never the WSL
  launcher) with LF-only fake tools, and the POSIX-only "squeue missing" test
  is skipped there. New tests cover the fixes above and that a fresh `login`
  replaces a stale session and command log, which Windows relies on in place
  of the systemd logout hook.

## [0.4.0] - 2026-07-18

### Added

- `jobs` command (in the **Tunnel** group): a cross-cluster overview of *your*
  Slurm jobs. With `-t <cluster>` it reports one cluster; without a target it
  sweeps every configured cluster and reports each with a live tunnel (down ones
  are noted, never logged in to). For each live cluster a bundled, universal
  probe (`scripts/jobs.sh`, shipped over the tunnel via `bash -s` — nothing to
  install on the cluster) runs `squeue` for your running + pending jobs and, by
  default, `sacct` for jobs that finished within a window (`-s/--since`, default
  `24h`; `--since 0` shows the live queue only). Each job is rendered in a
  per-cluster table (job ID, name, state, elapsed, time limit, partition,
  nodes), active jobs first, then finished newest-first. Read-only, so it is
  **not** budget-guarded; `-j/--json` gives machine-readable output.

### Changed

- Skill etiquette (`cluster-etiquette.md`): new **"Moving code and data"**
  guidance — sync **code with git** whenever possible (versioned, incremental,
  keeps both copies in sync), and move **large files and assets** (datasets,
  checkpoints, archives, run outputs) with the built-in `upload`/`download`
  transfer commands rather than committing them to the repo.

## [0.3.0] - 2026-07-10

### Added

- `requires_password` per-cluster config flag (default `true`, so existing
  configs are unchanged). Set it to `false` for a cluster that authenticates
  with an OTP and/or SSH key but **no service password**: the interactive login
  popup then omits the password field entirely and no password prompt is
  answered — mirroring how `requires_otp: false` hides the OTP field. When
  `true`, the popup now requires a non-empty password before it submits. The
  flag is surfaced in `ctun info` as `Password required: ✓/✗` (and as
  `requires_password` in the `--json` output).
- `CLAUDE.md`: repository guide for coding agents (project overview, file map,
  config schema, and the release process).

### Changed

- Skill guidance: reading `ctun -t <cluster> info` is now a firm per-cluster
  gate — an agent reads a cluster's briefing (description + advisory
  restrictions) before its first action on that cluster, every session, not
  only before submitting jobs.
- Skill etiquette (`cluster-etiquette.md`): safer directory-deletion practice
  and graceful shutdown/cancellation guidance for cleanup-heavy jobs.

## [0.2.0] - 2026-07-03

### Added

- `upload` and `download` commands: transfer files in/out of a cluster with
  **rsync over the live tunnel** (rides the existing master — no re-auth/OTP).
  `upload <local-src> <remote-dest>` / `download <remote-src> <local-dest>`,
  recursive (`-r`), remote paths written bare, `-n/--dry-run`, and extra rsync
  flags after `--`. Not budget-guarded; fails closed (exit 10) with no live
  tunnel. The transfer engine sits behind a backend abstraction so a scp/sftp
  fallback can be added later without changing the CLI.
- Distinct exit codes for `run` pre-flight failures: `10` (no live tunnel —
  login required), `11` (budget exhausted), `12` (budget guard could not
  verify), each also printing a machine-readable `ctun-error: <marker>` line to
  stderr, so a coding agent can branch on the failure without scraping text. The
  remote command's own exit code is still propagated unchanged, and `--dry-run`
  now exits with the would-be code so it doubles as a pre-flight check.
- Shell completion. The top-level `--init-completion <bash|zsh|fish>` option
  prints an activation script (mirroring `--version`), and `-t/--target`
  completes the cluster names from the active config — read live, so adding a
  cluster makes it completable immediately.
- `skill/monitoring-jobs.md`: guidance for live-monitoring a running Slurm job
  via the agent's `Monitor` tool over the tunnel — tracking Slurm state
  transitions and watching the log for progress and failures, with the rule that
  every terminal state must be surfaced ("silence is not success").
- `systemd/` logout-on-shutdown hook: a systemd **user** service whose
  `ExecStop` runs `ctun logout` on every logout, reboot, or power off, so live
  tunnels are torn down cleanly instead of leaving stale state. Includes an
  `install.sh` (install/`--remove`) and a README.
- `cluster-tunnel` agent **skill** under `skill/` — a Claude Code / Agent-Skills
  skill that teaches a coding agent to drive clusters through `ctun`: the
  startup workflow (status → info → interactive login → run), the per-session
  budget guard, and HPC etiquette (never compute on login nodes; always use
  Slurm). Includes `SKILL.md`, a full CLI `reference.md`, `cluster-etiquette.md`,
  and install instructions.
- `logs` command: print the timestamped commands sent to a cluster this session
  (requires `-t`; `-j/--json` for machine-readable output). Only the commands
  are recorded — never their output.
- Temporary per-cluster **command log** in the cache (`cmdlog`): records the
  commands sent over the tunnel (commands only), surfaced by `status` and
  `logs`, and cleared on `logout` and on a fresh `login`.
- `status` now shows, per cluster, the number of commands sent and the time of
  the last command, plus — for an explicitly targeted, live, guarded cluster
  only — the live **budget used** (with a safeguard message if the probe fails).
- `info` now reports connection details (host, ssh alias, user, whether **OTP**
  is required) and, when a budget is configured, the guard commands, probe
  fail-mode, and the path of the budget script.
- `requires_otp` per-cluster config field (advisory; shown by `info`).
- Single-letter aliases for subcommand options: `login -i/--interactive` and
  `-l/--limit`; `status` and `info` `-j/--json`; `run -n/--dry-run`;
  `config -i/--init`, `-p/--path`, `-s/--show`; `webui -p/--port` and
  `-n/--no-browser`.
- Single source of truth for the package version: a `cluster_tunnel/VERSION`
  file read at runtime via `constants.get_version()`, plus a
  `[tool.bumpversion]` (bump-my-version) workflow that keeps `pyproject.toml`
  and `VERSION` in sync — `uv run bump-my-version bump <patch|minor|major>`.
  `bump-my-version` is declared in the `dev` extra.

### Changed

- `logout` without a target now logs out **every configured cluster** (closing
  each tunnel and clearing its session + command log); pass `-t <cluster>` to
  log out just one.
- `config --init` now copies **all** bundled budget scripts from
  `budget_templates/` into the user's `budget/` directory under their real names
  (e.g. `budget/haicore.sh`), so a cluster referencing `budget/<cluster>.sh`
  works out of the box. Existing files are never overwritten. (Previously it
  copied only `horeka.sh.example`.)
- The default budget metric is now **job-hours** (Slurm job wall-clock time since
  the session started, clamped to the session window; concurrent jobs add up),
  computed from `sacct` and working on any cluster with Slurm accounting. The
  bundled `budget_templates/` scripts and the example config use `unit: jobh`
  (previously the haicore template reported GPU-hours and the horeka template
  CPU core-hours).
- `info` now renders each cluster as a Rich panel (description, connection,
  restrictions, budget) and, when no `-t` target is given, prints a panel for
  every configured cluster instead of requiring a target.
- `status` now stretches to the full terminal width.
- `login` and `logout` print nicer, clearer success/failure output (`login`
  success is a Rich panel showing the target and the session budget).
- `--version` and `cluster_tunnel.__version__` are now sourced from the
  `VERSION` file (previously the installed-package metadata and a hardcoded
  literal), so they can no longer drift from the packaged version.

- Grouped `info`, `config`, and `webui` under a single **Miscellaneous** panel in
  the main `--help` output (previously three separate Context/Configuration/Interface
  groups).
- Expanded the `login` help text with background on the persistent OpenSSH
  ControlMaster tunnel and the session-scoped compute-budget guard.
- Updated the README command synopses to reflect the new short option flags.
- The main `--help` banner is now a two-part logo lockup: an ANSI-art image
  (`logo_image.txt`) beside the text wordmark (`logo_text.txt`), replacing the
  single `logo.txt`. The image is rendered with its own ANSI colours (literal
  `\e`/`\033` escape sequences are normalised to real escapes) and the wordmark
  is colourised by region.
- The main `--help` description now summarises what `ctun` does — an
  authenticate-once persistent tunnel for you and your coding agents, plus the
  session-scoped compute-budget guard — and ends with a numbered **Getting
  started** walkthrough (`config --init` → `config` → `login` → `run`).
- The example config and docs now use a generic placeholder cluster username
  (`ab1234`) instead of a real account.
- The interactive login pop-up (`login --interactive`) now omits the OTP field
  for clusters configured with `requires_otp: false`, asking only for the
  password and session limit.

- `config --validate` now also reports advisory warnings that pydantic can't
  catch: a cluster whose `budget.script` doesn't exist on disk (the guard would
  fail — and with `fail_mode: closed` block every submission), and a
  `guard_commands` entry that isn't a valid regex (it would silently fall back
  to a literal match). The config is still reported as structurally valid, but
  the command exits non-zero when any warning is present.

- `info --json` now includes a top-level `ctun_version`, so an agent reading the
  briefing can record or condition on the tool version.
- `status --json` now always includes `target` (the ssh destination), `unit`,
  and `limit` at the top level of each cluster row — previously `unit`/`limit`
  were only reachable inside the nested `session` object (and absent when there
  was no session), forcing consumers to special-case missing keys.
- `run` now warns (in yellow) before the hard block when a guarded command
  pushes session usage to ≥80% of the limit — e.g. `budget: approaching limit
  — 90% used · …` — so an agent can pace submissions instead of hitting the wall
  unannounced. Below the threshold the usual gray budget line is shown.

### Fixed

- Bundled budget scripts (`budget_templates/{haicore,horeka}.sh`) now compute
  epoch seconds with `date` instead of `awk`'s gawk-only `mktime()`, so the
  budget probe works on clusters whose default `awk` is **mawk** (e.g.
  Debian/Ubuntu). Previously the probe failed there and, with the default
  `fail_mode: closed`, silently blocked every job submission. The README example
  budget script was updated to match.

## [0.1.0] - 2026-06-25

Initial MVP release.

### Added

- Persistent authenticated SSH tunnels via OpenSSH ControlMaster/ControlPersist:
  `login` authenticates once (password + OTP) and leaves a background master
  connection alive; later commands reuse it with no re-authentication.
- `run` to execute commands through the tunnel, streaming output and propagating
  the remote exit code; fails closed with a clear message when no tunnel is live.
- Session-scoped compute-budget guard: before a job-submitting command
  (`sbatch`/`srun`/`salloc`), a per-cluster budget script runs on the login node
  and the submission is blocked once the session limit is reached, with
  configurable fail-open/fail-closed behaviour on probe errors.
- `status`, `info`, and `logout` commands.
- YAML configuration with per-cluster settings, inherited defaults, advisory
  restrictions, and an agent preamble; managed via `config`
  (`--init`/`--path`/`--show`/`--validate`).
- Optional Tk pop-up login (`login --interactive`) so a present human can enter
  the password without exposing it to an automated caller (e.g. a coding agent).
- `webui` placeholder for a planned local web interface.
