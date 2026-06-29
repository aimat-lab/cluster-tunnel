# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Single-letter aliases for subcommand options: `login -i/--interactive` and
  `-l/--limit`; `status` and `info` `-j/--json`; `run -n/--dry-run`;
  `config -i/--init`, `-p/--path`, `-s/--show`; `webui -p/--port` and
  `-n/--no-browser`.

### Changed

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
