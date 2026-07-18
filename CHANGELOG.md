# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
