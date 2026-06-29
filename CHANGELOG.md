# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
